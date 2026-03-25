"""Per-model concurrency limiter.

Most LLM providers enforce concurrency limits based on plan tier.
A single asyncio.Semaphore per model is shared across ALL agents,
so the limit applies to the total in-flight calls for that model —
not per-agent.

Example: 5 agents all using MiniMax-M2.5 with limit=3 means at most
3 of those 5 can be in the middle of an LLM call simultaneously.
The other 2 wait their turn.

Per-model limits are configured under each provider's ``models:`` block
in pyclaw.yaml (e.g. providers.minimax.models.MiniMax-M2.5.concurrency).
The ``concurrency.default`` setting is a global fallback for any model
not explicitly listed.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional
from pyclaw.reflect import reflect_system

from pyclaw.core.usage import ThrottledError, get_registry

logger = logging.getLogger("pyclaw.concurrency")

# Default if neither config nor per-model limit is set
_GLOBAL_DEFAULT = 3


@reflect_system("concurrency")
class ModelConcurrencyManager:
    """Shared per-model asyncio semaphores for LLM call throttling.

    Maintains one asyncio.Semaphore per model string, shared across all
    agents in the process so the configured limit applies to total in-flight
    calls — not per-agent.  Semaphore creation is lazy (on first use).

    Thread-safe: the internal dicts are only written from the asyncio event
    loop; no additional locking is required.

    Attributes:
        _default (int): Fallback concurrency limit for unconfigured models.
        _limits (Dict[str, int]): Per-model concurrency limits.
        _semaphores (Dict[str, asyncio.Semaphore]): Active semaphore instances.
    """

    def __init__(
        self,
        model_limits: Optional[Dict[str, int]] = None,
        default: int = _GLOBAL_DEFAULT,
    ):
        """Initialize the ModelConcurrencyManager.

        Args:
            model_limits (Optional[Dict[str, int]]): Per-model concurrency limits
                keyed by model name. Defaults to an empty dict.
            default (int): Global fallback limit for models not in model_limits.
                Defaults to 3.
        """
        self._default = default
        self._limits: Dict[str, int] = dict(model_limits or {})
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(self, model_limits: Dict[str, int], default: int) -> None:
        """Update per-model limits and the global default from config.

        Call this before the first ``acquire()`` to apply config-file values.
        Merges new limits into existing ones; does not create semaphores.

        Args:
            model_limits (Dict[str, int]): Per-model concurrency limits.
            default (int): New global default limit.
        """
        self._limits.update(model_limits)
        self._default = default

    def limit_for(self, model: str) -> int:
        """Return the concurrency limit for a model.

        Resolves in order: exact model name → base name (strips provider
        prefix before the last ``/``) → global default.

        Args:
            model (str): Model identifier, e.g. ``"minimax/MiniMax-M2.5"`` or
                ``"MiniMax-M2.5"``.

        Returns:
            int: Configured concurrency limit for the model.
        """
        # Exact match first, then try base model name (strip provider prefix)
        base = model.split("/")[-1] if "/" in model else model
        return (
            self._limits.get(model)
            or self._limits.get(base)
            or self._default
        )

    def semaphore_for(self, model: str) -> asyncio.Semaphore:
        """Get (or lazily create) the semaphore for a model.

        Creates a new asyncio.Semaphore with the resolved limit on first call
        for each model string.

        Args:
            model (str): Model identifier.

        Returns:
            asyncio.Semaphore: The semaphore controlling in-flight calls for
                this model.
        """
        if model not in self._semaphores:
            limit = self.limit_for(model)
            self._semaphores[model] = asyncio.Semaphore(limit)
            logger.debug(f"Created semaphore for {model!r}: limit={limit}")
        return self._semaphores[model]

    @asynccontextmanager
    async def acquire(self, model: str, priority: str = "critical") -> AsyncIterator[None]:
        """Async context manager that acquires a concurrency slot for a model.

        Blocks until a slot is available, yields control to the caller, then
        releases the slot on exit.  Example::

            async with concurrency.acquire("MiniMax-M2.5"):
                response = await llm_call(...)

            # background task — throttled when provider usage is high:
            async with concurrency.acquire("zai/glm-4.7", priority="background"):
                response = await llm_call(...)

        Args:
            model (str): Model identifier to acquire a slot for.
            priority (str): Request priority — ``"critical"`` (chat, never
                throttled), ``"normal"`` (jobs), or ``"background"`` (vault
                ingestion).  Defaults to ``"critical"``.

        Raises:
            ThrottledError: If the provider usage exceeds the configured
                threshold for *priority* (non-critical only).

        Yields:
            None: Control is yielded while the slot is held.
        """
        # Usage check before waiting for the concurrency slot
        get_registry().check(model, priority)

        sem = self.semaphore_for(model)
        waiting = sem._value == 0  # type: ignore[attr-defined]
        if waiting:
            logger.debug(f"Waiting for concurrency slot: {model!r}")
        async with sem:
            yield

    def status(self) -> Dict[str, Dict]:
        """Return current semaphore state for all active models.

        Used by the TUI Traces view and the ``/api/v1/status`` endpoint to
        surface real-time concurrency usage.

        Returns:
            Dict[str, Dict]: Mapping of model name to a dict with keys:
                ``limit`` (int), ``in_flight`` (int), ``waiting`` (int).
        """
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
    """Return the process-wide concurrency manager, creating it if necessary.

    The singleton is created with default settings on first call and can be
    replaced with configured limits by calling init_manager() afterward.

    Returns:
        ModelConcurrencyManager: The active global manager instance.
    """
    global _manager
    if _manager is None:
        _manager = ModelConcurrencyManager()
    return _manager


def init_manager(model_limits: Dict[str, int], default: int = _GLOBAL_DEFAULT) -> ModelConcurrencyManager:
    """Initialize (or reinitialize) the global manager from config.

    Replaces any existing manager singleton.  Should be called during gateway
    startup before any AgentRunner is initialized.

    Args:
        model_limits (Dict[str, int]): Per-model concurrency limits.
        default (int): Global fallback limit. Defaults to 3.

    Returns:
        ModelConcurrencyManager: The newly created global manager instance.
    """
    global _manager
    _manager = ModelConcurrencyManager(model_limits=model_limits, default=default)
    logger.info(
        f"Concurrency manager initialized: default={default}, "
        f"models={model_limits}"
    )
    return _manager
