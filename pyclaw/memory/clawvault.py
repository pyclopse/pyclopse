"""ClawVault implementation of MemoryBackend."""

import logging
from typing import Any, Dict, List, Optional

from .backend import MemoryBackend
from .client import ClawVaultClient

logger = logging.getLogger("pyclaw.memory")


class ClawVaultBackend(MemoryBackend):
    """Default memory backend backed by the ClawVault CLI.

    This wraps the existing :class:`ClawVaultClient` and maps the abstract
    :class:`MemoryBackend` interface onto ClawVault's subprocess commands.

    ClawVault's native interface does not map 1-to-1 onto a simple key-value
    store.  This implementation uses a best-effort mapping:

    - ``read``   → ``recall(key, limit=1)``
    - ``write``  → ``store({key, ...value})``
    - ``delete`` → not natively supported; logs a warning and returns False
    - ``search`` → ``vsearch(query)``
    - ``list``   → ``graph()`` keys extraction

    Attributes:
        _client (ClawVaultClient): The underlying subprocess wrapper used
            to invoke ClawVault CLI commands.
    """

    def __init__(self, vault_path: str = "~/.claw/vault") -> None:
        """Initialise the backend with the path to the ClawVault vault.

        Args:
            vault_path (str): Path to the ClawVault vault directory.
                Defaults to ``"~/.claw/vault"``.
        """
        self._client = ClawVaultClient(vault_path)

    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        """Read the most recent memory entry for *key* via ClawVault recall.

        Args:
            key (str): The session ID or memory key to look up.

        Returns:
            Optional[Dict[str, Any]]: The first recalled entry dict, or None
                if nothing was found.
        """
        results = await self._client.recall(key, limit=1)
        return results[0] if results else None

    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        """Store a memory entry in ClawVault.

        Merges *key* into *value* and passes the combined dict to
        :meth:`ClawVaultClient.store`.

        Args:
            key (str): The memory key / identifier for the entry.
            value (Dict[str, Any]): Additional fields to store alongside
                the key.

        Returns:
            bool: True if the store command succeeded, False otherwise.
        """
        return await self._client.store({"key": key, **value})

    async def delete(self, key: str) -> bool:
        """Attempt to delete a memory entry (not supported by ClawVault).

        ClawVault entries are immutable observations; the CLI has no delete
        command.  This method always logs a warning and returns False.
        Callers that require true deletion should register a
        ``memory:delete`` hook handler using a different backend.

        Args:
            key (str): The key that was requested for deletion.

        Returns:
            bool: Always False.
        """
        # ClawVault CLI has no delete command — entries are immutable observations.
        # Plugins that need true delete should provide an alternative backend.
        logger.warning(
            f"ClawVaultBackend.delete('{key}'): ClawVault does not support "
            "deletion.  Install a plugin that provides a memory:delete handler "
            "to enable this operation."
        )
        return False

    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search the ClawVault using its built-in vector search command.

        Args:
            query (str): Natural-language search query.
            limit (int): Maximum number of results to return. Defaults to 10.
            **kwargs (Any): Unused; accepted for interface compatibility.

        Returns:
            List[Dict[str, Any]]: Ordered list of matching entry dicts.
        """
        return await self._client.search(query, limit=limit)

    async def list(self, prefix: str = "") -> List[str]:
        """List all keys from the ClawVault memory graph.

        Retrieves the full graph via :meth:`ClawVaultClient.graph` and
        extracts the top-level keys, optionally filtering by *prefix*.

        Args:
            prefix (str): Optional key prefix filter. Defaults to ``""``
                (return all keys).

        Returns:
            List[str]: Filtered list of key strings from the vault graph.
        """
        graph = await self._client.graph()
        keys = list(graph.keys()) if isinstance(graph, dict) else []
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys
