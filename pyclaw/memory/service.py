"""
MemoryService — routes memory operations through the hook registry.

Plugins replace the default ClawVault backend by registering handlers for
the ``memory:*`` interceptable events.  If no plugin handles a call, the
service falls back to the configured default backend.
"""

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from pyclaw.hooks.events import HookEvent

if TYPE_CHECKING:
    from pyclaw.hooks.registry import HookRegistry
    from .backend import MemoryBackend

logger = logging.getLogger("pyclaw.memory")

# Module-level singleton (set by gateway during initialisation)
_instance: Optional["MemoryService"] = None


def get_memory_service() -> Optional["MemoryService"]:
    """Return the global MemoryService instance, or None if not initialised."""
    return _instance


def set_memory_service(service: "MemoryService") -> None:
    """Set the global MemoryService instance (called by gateway init)."""
    global _instance
    _instance = service


class MemoryService:
    """
    Routes memory CRUD operations through the HookRegistry.

    For each operation the service fires the corresponding ``memory:*``
    interceptable hook.  If a plugin handler returns a non-None result,
    that result is used.  Otherwise the default backend is called.

    This is the single place all memory access should go through — MCP
    tools, agent tools, and internal code should all use this service.
    """

    def __init__(
        self,
        registry: "HookRegistry",
        default_backend: "MemoryBackend",
    ) -> None:
        self._registry = registry
        self._default = default_backend

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        result = await self._registry.intercept(
            HookEvent.MEMORY_READ, {"key": key}
        )
        if result is not None:
            return result
        return await self._default.read(key)

    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        result = await self._registry.intercept(
            HookEvent.MEMORY_WRITE, {"key": key, "value": value}
        )
        if result is not None:
            return bool(result)
        return await self._default.write(key, value)

    async def delete(self, key: str) -> bool:
        result = await self._registry.intercept(
            HookEvent.MEMORY_DELETE, {"key": key}
        )
        if result is not None:
            return bool(result)
        return await self._default.delete(key)

    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        result = await self._registry.intercept(
            HookEvent.MEMORY_SEARCH,
            {"query": query, "limit": limit, **kwargs},
        )
        if result is not None:
            return result
        return await self._default.search(query, limit=limit, **kwargs)

    async def list(self, prefix: str = "") -> List[str]:
        result = await self._registry.intercept(
            HookEvent.MEMORY_LIST, {"prefix": prefix}
        )
        if result is not None:
            return result
        return await self._default.list(prefix)
