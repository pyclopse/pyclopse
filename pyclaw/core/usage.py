"""Provider usage monitoring and throttling.

Each ``GenericProviderConfig`` that has a ``usage:`` block gets a
``UsageMonitor`` which periodically polls the provider's usage endpoint
and caches the result.  The module-level ``UsageRegistry`` maps provider
names to their monitors and is the single authority for throttle decisions.

Priority levels
---------------
``critical``
    Chat messages — never throttled.
``normal``
    Scheduled jobs — throttled when usage >= ``throttle.normal`` (default 90 %).
``background``
    Vault ingestion, bulk ingest — throttled when usage >= ``throttle.background``
    (default 70 %).

Throttling behaviour
--------------------
When a caller's priority is throttled, :meth:`acquire` raises
:class:`ThrottledError`.  The caller decides what to do (log and bail,
reschedule, etc.).  Chat messages pass through unconditionally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("pyclaw.usage")


class ThrottledError(Exception):
    """Raised when a request is blocked due to provider usage limits."""

    def __init__(self, provider: str, priority: str, usage_pct: float, threshold: int) -> None:
        self.provider = provider
        self.priority = priority
        self.usage_pct = usage_pct
        self.threshold = threshold
        super().__init__(
            f"Provider '{provider}' is at {usage_pct:.1f}% usage "
            f"(threshold for '{priority}' priority: {threshold}%)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# JSON path resolution
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_path(data: Any, path: str) -> Any:
    """Resolve a dot-notation path against *data*.

    Integer path segments are treated as list indices::

        _resolve_path(resp, "model_remains.0.current_interval_total_count")
    """
    node = data
    for segment in path.split("."):
        if node is None:
            return None
        if isinstance(node, list):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(segment)
        else:
            return None
    return node


# ──────────────────────────────────────────────────────────────────────────────
# UsageMonitor
# ──────────────────────────────────────────────────────────────────────────────

class UsageMonitor:
    """Polls a provider's usage endpoint and exposes cached usage %.

    Args:
        provider_name: Human-readable name used in log messages.
        api_key: Bearer token for the usage endpoint.
        config: :class:`~pyclaw.config.schema.UsageConfig` instance.
    """

    def __init__(self, provider_name: str, api_key: Optional[str], config) -> None:
        self._provider = provider_name
        self._api_key = api_key
        self._config = config
        self._usage_pct: Optional[float] = None
        self._last_poll: float = 0.0
        self._task: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background polling loop."""
        await self._poll()  # immediate first fetch
        self._task = asyncio.create_task(self._loop(), name=f"usage-{self._provider}")

    async def stop(self) -> None:
        """Cancel background polling loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def usage_percent(self) -> Optional[float]:
        """Cached usage percentage (0–100), or ``None`` if unavailable."""
        return self._usage_pct

    def is_throttled(self, priority: str) -> bool:
        """Return True if *priority* should be blocked given current usage.

        ``"critical"`` is never throttled.
        """
        if priority == "critical" or self._usage_pct is None:
            return False
        cfg = self._config
        throttle = cfg.throttle
        if priority == "background":
            return self._usage_pct >= throttle.background
        if priority == "normal":
            return self._usage_pct >= throttle.normal
        return False

    def status_dict(self) -> Dict[str, Any]:
        """Return a dict suitable for /status display."""
        age = int(time.time() - self._last_poll) if self._last_poll else None
        return {
            "usage_pct": round(self._usage_pct, 1) if self._usage_pct is not None else None,
            "last_poll_seconds_ago": age,
            "check_interval": self._config.check_interval,
            "endpoint": self._config.endpoint,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        interval = self._config.check_interval
        while True:
            await asyncio.sleep(interval)
            await self._poll()

    async def _poll(self) -> None:
        cfg = self._config
        headers: Dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    cfg.endpoint,
                    headers=headers,
                    params=cfg.params or {},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Usage poll failed for '%s': %s", self._provider, exc)
            return

        pct = self._parse_percent(data)
        if pct is not None:
            self._usage_pct = max(0.0, min(100.0, pct))
            self._last_poll = time.time()
            logger.debug(
                "Provider '%s' usage: %.1f%%", self._provider, self._usage_pct
            )

    def _parse_percent(self, data: Any) -> Optional[float]:
        """Extract a 0-100 usage percentage from the JSON response."""
        cfg = self._config

        # Direct percent path
        if cfg.percent_path:
            val = _resolve_path(data, cfg.percent_path)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass

        # total + remaining
        if cfg.total_path and cfg.remaining_path:
            total = _resolve_path(data, cfg.total_path)
            remaining = _resolve_path(data, cfg.remaining_path)
            try:
                total = float(total)
                remaining = float(remaining)
                if total > 0:
                    return (total - remaining) / total * 100
            except (TypeError, ValueError):
                pass

        # total + used
        if cfg.total_path and cfg.used_path:
            total = _resolve_path(data, cfg.total_path)
            used = _resolve_path(data, cfg.used_path)
            try:
                total = float(total)
                used = float(used)
                if total > 0:
                    return used / total * 100
            except (TypeError, ValueError):
                pass

        logger.warning(
            "Could not parse usage response for '%s': no matching path found. "
            "Check usage.percent_path / total_path / remaining_path in config.",
            self._provider,
        )
        return None


# ──────────────────────────────────────────────────────────────────────────────
# UsageRegistry
# ──────────────────────────────────────────────────────────────────────────────

class UsageRegistry:
    """Holds all UsageMonitor instances and answers throttle queries."""

    def __init__(self) -> None:
        self._monitors: Dict[str, UsageMonitor] = {}
        # model_name → provider_name (for throttle lookups)
        self._model_provider: Dict[str, str] = {}

    def register(
        self,
        provider_name: str,
        monitor: UsageMonitor,
        model_names: Optional[list] = None,
    ) -> None:
        """Register *monitor* for *provider_name* (and optionally its models)."""
        self._monitors[provider_name] = monitor
        for model in model_names or []:
            self._model_provider[model] = provider_name

    def get(self, provider_name: str) -> Optional[UsageMonitor]:
        return self._monitors.get(provider_name)

    def monitor_for_model(self, model: str) -> Optional[UsageMonitor]:
        """Return the monitor responsible for *model*, or None."""
        # Try exact match, then strip provider prefix ("zai/glm-4.7" → "glm-4.7")
        base = model.split("/")[-1] if "/" in model else model
        provider = self._model_provider.get(model) or self._model_provider.get(base)
        if provider:
            return self._monitors.get(provider)
        # Fallback: try matching provider name directly in the model string
        # e.g. "zai/glm-4.7" → check if "zai" has a monitor
        if "/" in model:
            prefix = model.split("/")[0]
            if prefix in self._monitors:
                return self._monitors[prefix]
        return None

    def is_throttled(self, model: str, priority: str) -> bool:
        """Return True if *priority* is throttled for the provider of *model*."""
        if priority == "critical":
            return False
        monitor = self.monitor_for_model(model)
        if monitor is None:
            return False
        return monitor.is_throttled(priority)

    def check(self, model: str, priority: str) -> None:
        """Raise :class:`ThrottledError` if *priority* is throttled for *model*."""
        if priority == "critical":
            return
        monitor = self.monitor_for_model(model)
        if monitor is None:
            return
        if monitor.is_throttled(priority):
            pct = monitor.usage_percent() or 0.0
            threshold = (
                monitor._config.throttle.background
                if priority == "background"
                else monitor._config.throttle.normal
            )
            # Determine provider name
            base = model.split("/")[-1] if "/" in model else model
            provider = (
                self._model_provider.get(model)
                or self._model_provider.get(base)
                or model.split("/")[0] if "/" in model else model
            )
            raise ThrottledError(provider, priority, pct, threshold)

    async def start_all(self) -> None:
        for name, monitor in self._monitors.items():
            try:
                await monitor.start()
                logger.info("Usage monitor started for provider '%s'", name)
            except Exception as exc:
                logger.warning("Failed to start usage monitor for '%s': %s", name, exc)

    async def stop_all(self) -> None:
        for monitor in self._monitors.values():
            await monitor.stop()

    def status(self) -> Dict[str, Any]:
        """Return usage status dict for all monitored providers."""
        return {
            name: monitor.status_dict()
            for name, monitor in self._monitors.items()
        }


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_registry: Optional[UsageRegistry] = None


def get_registry() -> UsageRegistry:
    """Return the process-wide UsageRegistry (lazily created)."""
    global _registry
    if _registry is None:
        _registry = UsageRegistry()
    return _registry


def init_registry(providers_cfg) -> UsageRegistry:
    """Build and return a new registry from *providers_cfg*.

    Iterates all ``GenericProviderConfig`` instances that have ``usage:``
    configured and creates a ``UsageMonitor`` for each.

    Args:
        providers_cfg: :class:`~pyclaw.config.schema.ProvidersConfig` instance.

    Returns:
        The newly created :class:`UsageRegistry` singleton.
    """
    from pyclaw.config.schema import GenericProviderConfig

    global _registry
    _registry = UsageRegistry()

    # Build provider_name → config mapping (typed fields + model_extra)
    all_providers: Dict[str, Any] = {}
    for field_name in ("openai", "anthropic", "google", "fastagent", "minimax"):
        val = getattr(providers_cfg, field_name, None)
        if val is not None:
            all_providers[field_name] = val
    for name, val in (getattr(providers_cfg, "model_extra", None) or {}).items():
        all_providers[name] = val

    for provider_name, pcfg in all_providers.items():
        if not isinstance(pcfg, GenericProviderConfig):
            continue
        usage_cfg = getattr(pcfg, "usage", None)
        if usage_cfg is None or not usage_cfg.enabled:
            continue
        # Resolve api_key: usage config key first, fall back to provider key
        api_key = usage_cfg.api_key or pcfg.api_key
        model_names = list(getattr(pcfg, "models", {}).keys())
        monitor = UsageMonitor(
            provider_name=provider_name,
            api_key=api_key,
            config=usage_cfg,
        )
        _registry.register(provider_name, monitor, model_names)
        logger.info(
            "Registered usage monitor for provider '%s' (endpoint: %s)",
            provider_name,
            usage_cfg.endpoint,
        )

    return _registry
