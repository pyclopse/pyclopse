"""
Per-model concurrency limiter.

Most LLM providers enforce concurrency limits based on plan tier.
A single asyncio.Semaphore per model is shared across ALL agents,
so the limit applies to the total in-flight calls for that model —
not per-agent.

Example: 5 agents all using MiniMax-M2.5 with limit=3 means at most
3 of those 5 can be in the middle of an LLM call simultaneously.
The other 2 wait their turn.

Config (pyclaw.yaml):
    concurrency:
      default: 3          # fallback for any model not listed
      models:
        MiniMax-M2.5: 3
        passthrough: 100  # effectively unlimited for local testing
        gpt-4: 5
        claude-sonnet-4-5: 5
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

logger = logging.getLogger("pyclaw.concurrency")

# Default if neither config nor per-model limit is set
_GLOBAL_DEFAULT = 3


class ModelConcurrencyManager:
    """
    Shared per-model asyncio semaphores.

    Thread-safe: semaphore creation uses a dict that is only ever
    written from the asyncio event loop.
    """

    def __init__(
        self,
        model_limits: Optional[Dict[str, int]] = None,
        default: int = _GLOBAL_DEFAULT,
    ):
        self._default = default
        self._limits: Dict[str, int] = dict(model_limits or {})
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(self, model_limits: Dict[str, int], default: int) -> None:
        """Update limits from config (call before first use)."""
        self._limits.update(model_limits)
        self._default = default

    def limit_for(self, model: str) -> int:
        """Return the concurrency limit for a model."""
        # Exact match first, then try base model name (strip provider prefix)
        base = model.split("/")[-1] if "/" in model else model
        return (
            self._limits.get(model)
            or self._limits.get(base)
            or self._default
        )

    def semaphore_for(self, model: str) -> asyncio.Semaphore:
        """Get (or lazily create) the semaphore for a model."""
        if model not in self._semaphores:
            limit = self.limit_for(model)
            self._semaphores[model] = asyncio.Semaphore(limit)
            logger.debug(f"Created semaphore for {model!r}: limit={limit}")
        return self._semaphores[model]

    @asynccontextmanager
    async def acquire(self, model: str) -> AsyncIterator[None]:
        """
        Async context manager — acquire the slot, yield, release.

            async with concurrency.acquire("MiniMax-M2.5"):
                response = await llm_call(...)
        """
        sem = self.semaphore_for(model)
        waiting = sem._value == 0  # type: ignore[attr-defined]
        if waiting:
            logger.debug(f"Waiting for concurrency slot: {model!r}")
        async with sem:
            yield

    def status(self) -> Dict[str, Dict]:
        """Return current semaphore state (for TUI / status endpoint)."""
        out = {}
        for model, sem in self._semaphores.items():
            limit = self.limit_for(model)
            in_flight = limit - sem._value  # type: ignore[attr-defined]
            out[model] = {
                "limit": limit,
                "in_flight": in_flight,
                "waiting": max(0, in_flight - limit),
            }
        return out


# ---------------------------------------------------------------------------
# Module-level singleton — shared by all AgentRunners in the process
# ---------------------------------------------------------------------------
_manager: Optional[ModelConcurrencyManager] = None


def get_manager() -> ModelConcurrencyManager:
    """Return the process-wide concurrency manager (create if needed)."""
    global _manager
    if _manager is None:
        _manager = ModelConcurrencyManager()
    return _manager


def init_manager(model_limits: Dict[str, int], default: int = _GLOBAL_DEFAULT) -> ModelConcurrencyManager:
    """Initialize (or reinitialize) the global manager from config."""
    global _manager
    _manager = ModelConcurrencyManager(model_limits=model_limits, default=default)
    logger.info(
        f"Concurrency manager initialized: default={default}, "
        f"models={model_limits}"
    )
    return _manager
