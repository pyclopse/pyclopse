"""ClawVault implementation of MemoryBackend."""

import logging
from typing import Any, Dict, List, Optional

from .backend import MemoryBackend
from .client import ClawVaultClient

logger = logging.getLogger("pyclaw.memory")


class ClawVaultBackend(MemoryBackend):
    """
    Default memory backend backed by the ClawVault CLI.

    This wraps the existing ClawVaultClient and maps the abstract
    MemoryBackend interface onto ClawVault's subprocess commands.

    Note: ClawVault's native interface doesn't map 1-to-1 onto a simple
    key-value store.  This implementation does a best-effort mapping:

    - read   → recall(key, limit=1)
    - write  → store({key, ...value})
    - delete → not natively supported; logs a warning
    - search → vsearch(query)
    - list   → graph() keys extraction
    """

    def __init__(self, vault_path: str = "~/.claw/vault") -> None:
        self._client = ClawVaultClient(vault_path)

    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        results = await self._client.recall(key, limit=1)
        return results[0] if results else None

    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        return await self._client.store({"key": key, **value})

    async def delete(self, key: str) -> bool:
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
        return await self._client.search(query, limit=limit)

    async def list(self, prefix: str = "") -> List[str]:
        graph = await self._client.graph()
        keys = list(graph.keys()) if isinstance(graph, dict) else []
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys
