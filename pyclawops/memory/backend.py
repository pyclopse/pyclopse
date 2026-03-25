"""Abstract memory backend interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MemoryBackend(ABC):
    """Abstract interface for pyclawops's persistent memory store.

    All memory operations (read, write, delete, search, list) are routed
    through this interface.  The default (and only built-in) implementation is
    FileMemoryBackend (per-agent markdown daily journals).  Alternative backends
    (SQLite, vector DBs, remote APIs, etc.) can be swapped in by registering
    handlers for the ``memory:*`` hook events — the MemoryService routes each
    operation through the hook registry and only falls back to this backend if
    no plugin intercepts the call.
    """

    @abstractmethod
    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        """Read a memory entry by key.

        Implementors must look up the entry identified by *key* in whatever
        underlying store the backend uses and return it as a dict, or
        ``None`` if no such entry exists.

        Args:
            key (str): The unique identifier of the memory entry to retrieve.

        Returns:
            Optional[Dict[str, Any]]: The entry dict (shape is
                backend-defined), or None if not found.
        """

    @abstractmethod
    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        """Create or update a memory entry.

        Implementors must persist the *value* dict under *key*, creating a
        new entry if *key* does not yet exist or replacing/updating the
        existing one.

        Args:
            key (str): The unique identifier for the memory entry.
            value (Dict[str, Any]): A dict describing the entry.  Backends
                should expect at least a ``"content"`` key; additional fields
                (e.g. ``"tags"``) are optional.

        Returns:
            bool: True on success, False on failure.
        """

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a memory entry by key.

        Implementors must remove the entry identified by *key* from the
        underlying store.  If the backend does not support deletion it should
        log a warning and return ``False``.

        Args:
            key (str): The unique identifier of the memory entry to delete.

        Returns:
            bool: True on success, False if not found or on error.
        """

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search memory entries by semantic similarity or keyword.

        Implementors should rank results by relevance to *query*, using
        whatever search strategy the backend supports (keyword frequency,
        cosine similarity, full-text search, etc.).

        Args:
            query (str): Natural-language or keyword search string.
            limit (int): Maximum number of results to return. Defaults to 10.
            **kwargs (Any): Additional backend-specific parameters.

        Returns:
            List[Dict[str, Any]]: Matching entry dicts ordered by relevance
                (most relevant first).
        """

    @abstractmethod
    async def list(self, prefix: str = "") -> List[str]:
        """List all memory keys, optionally filtered by prefix.

        Implementors must return the full set of stored key strings,
        optionally restricting to those that begin with *prefix*.

        Args:
            prefix (str): Key prefix filter. Pass ``""`` (the default) to
                list all keys.

        Returns:
            List[str]: All matching key strings.
        """
