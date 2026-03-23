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
    """Return the global MemoryService instance, or None if not initialised.

    Returns:
        MemoryService: The module-level singleton, or None if
            ``set_memory_service`` has not yet been called.
    """
    return _instance


def set_memory_service(service: "MemoryService") -> None:
    """Set the global MemoryService instance (called by gateway init).

    Args:
        service (MemoryService): The fully initialised service to install as
            the module-level singleton.
    """
    global _instance
    _instance = service


class MemoryService:
    """Routes memory CRUD operations through the HookRegistry.

    For each operation the service fires the corresponding ``memory:*``
    interceptable hook.  If a plugin handler returns a non-None result,
    that result is used.  Otherwise the default backend is called.

    This is the single place all memory access should go through — MCP
    tools, agent tools, and internal code should all use this service.

    Attributes:
        _registry (HookRegistry): The hook registry used to dispatch
            interceptable ``memory:*`` events to registered plugins.
        _default (MemoryBackend): The fallback backend called when no
            plugin intercepts an operation.
    """

    def __init__(
        self,
        registry: "HookRegistry",
        default_backend: "MemoryBackend",
    ) -> None:
        """Initialise the service with a hook registry and a default backend.

        Args:
            registry (HookRegistry): Hook registry through which all
                ``memory:*`` events are dispatched.
            default_backend (MemoryBackend): Backend to use when no plugin
                intercepts an operation.
        """
        self._registry = registry
        self._default = default_backend

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        """Read a memory entry by key, routing through the hook registry.

        Fires ``HookEvent.MEMORY_READ``.  If a plugin returns a non-None
        result it is used directly; otherwise the default backend is queried.

        Args:
            key (str): The memory key to look up.

        Returns:
            Optional[Dict[str, Any]]: The entry dict, or None if not found.
        """
        result = await self._registry.intercept(
            HookEvent.MEMORY_READ, {"key": key}
        )
        if result is not None:
            return result
        return await self._default.read(key)

    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        """Create or update a memory entry, routing through the hook registry.

        Fires ``HookEvent.MEMORY_WRITE``.  If a plugin returns a non-None
        result it is coerced to bool and returned; otherwise the default
        backend handles the write.

        Args:
            key (str): The memory key to write.
            value (Dict[str, Any]): A dict containing at minimum a
                ``"content"`` field and an optional ``"tags"`` list.

        Returns:
            bool: True on success.
        """
        result = await self._registry.intercept(
            HookEvent.MEMORY_WRITE, {"key": key, "value": value}
        )
        if result is not None:
            return bool(result)
        return await self._default.write(key, value)

    async def delete(self, key: str) -> bool:
        """Delete a memory entry by key, routing through the hook registry.

        Fires ``HookEvent.MEMORY_DELETE``.  If a plugin returns a non-None
        result it is coerced to bool and returned; otherwise the default
        backend handles the deletion.

        Args:
            key (str): The memory key to delete.

        Returns:
            bool: True on success, False if not found or unsupported.
        """
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
        """Search memory entries, routing through the hook registry.

        Fires ``HookEvent.MEMORY_SEARCH``.  If a plugin returns a non-None
        result it is returned directly; otherwise the default backend
        performs the search.

        Args:
            query (str): Natural-language or keyword search query.
            limit (int): Maximum number of results to return. Defaults to 10.
            **kwargs (Any): Additional backend-specific search parameters
                passed through to the hook payload and backend.

        Returns:
            List[Dict[str, Any]]: Ordered list of matching entry dicts.
        """
        result = await self._registry.intercept(
            HookEvent.MEMORY_SEARCH,
            {"query": query, "limit": limit, **kwargs},
        )
        if result is not None:
            return result
        return await self._default.search(query, limit=limit, **kwargs)

    async def list(self, prefix: str = "") -> List[str]:
        """List memory keys, routing through the hook registry.

        Fires ``HookEvent.MEMORY_LIST``.  If a plugin returns a non-None
        result it is returned directly; otherwise the default backend
        lists the keys.

        Args:
            prefix (str): Optional key prefix filter. Defaults to ``""``
                (return all keys).

        Returns:
            List[str]: All matching memory key strings.
        """
        result = await self._registry.intercept(
            HookEvent.MEMORY_LIST, {"prefix": prefix}
        )
        if result is not None:
            return result
        return await self._default.list(prefix)
