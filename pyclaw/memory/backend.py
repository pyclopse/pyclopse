"""Abstract memory backend interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MemoryBackend(ABC):
    """
    Abstract interface for pyclaw's persistent memory store.

    All memory operations (read, write, delete, search, list) are routed
    through this interface.  The default implementation uses ClawVault.
    Alternative backends (SQLite, vector DBs, remote APIs, etc.) can be
    swapped in by registering handlers for the ``memory:*`` hook events —
    the MemoryService routes each operation through the hook registry and
    only falls back to this backend if no plugin intercepts the call.
    """

    @abstractmethod
    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Read a memory entry by key.

        Returns:
            The entry dict, or None if not found.
        """

    @abstractmethod
    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        """
        Create or update a memory entry.

        Returns:
            True on success.
        """

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """
        Delete a memory entry by key.

        Returns:
            True on success, False if not found or on error.
        """

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """
        Search memory entries by semantic similarity or keyword.

        Returns:
            List of matching entry dicts, ordered by relevance.
        """

    @abstractmethod
    async def list(self, prefix: str = "") -> List[str]:
        """
        List all memory keys, optionally filtered by prefix.

        Returns:
            List of key strings.
        """
