"""ClawVault CLI wrapper - subprocess interface to clawvault."""
import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pyclaw.memory")


class ClawVaultClient:
    """Subprocess wrapper around the clawvault CLI (npm package).

    ClawVault is accessed via subprocess calls rather than HTTP.  All public
    methods are async and spawn the ``clawvault`` binary using
    ``asyncio.create_subprocess_exec``.

    Attributes:
        vault_path (Path): Resolved absolute path to the ClawVault vault
            directory.
    """

    def __init__(self, vault_path: str = "~/.claw/vault") -> None:
        """Initialise the client and verify the clawvault CLI is installed.

        Emits a warning at module-load time if the ``clawvault`` binary
        cannot be found on PATH.

        Args:
            vault_path (str): Path to the vault directory. Tilde expansion is
                applied. Defaults to ``"~/.claw/vault"``.
        """
        self.vault_path = Path(vault_path).expanduser()
        self._check_installation()

    def _check_installation(self) -> None:
        """Check if the clawvault CLI is installed and log a warning if not.

        Runs ``clawvault --version`` synchronously (with a 5 s timeout).  On
        a non-zero exit code or ``FileNotFoundError``, logs a warning
        recommending ``npm install -g clawvault``.
        """
        try:
            result = subprocess.run(
                ["clawvault", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning(
                    "clawvault CLI not found. Install with: npm install -g clawvault"
                )
        except FileNotFoundError:
            logger.warning(
                "clawvault CLI not found. Install with: npm install -g clawvault"
            )

    async def _run_command(
        self,
        args: List[str],
        input_data: Optional[str] = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Run a clawvault subcommand asynchronously and return the result.

        Prepends ``"clawvault"`` to *args*, spawns a subprocess, and waits
        up to *timeout* seconds for it to finish.  The returned object mimics
        :class:`subprocess.CompletedProcess` with decoded ``stdout`` and
        ``stderr`` strings.

        Args:
            args (List[str]): Subcommand and flags to pass after
                ``"clawvault"``, e.g. ``["recall", "my-session", "--limit", "5"]``.
            input_data (Optional[str]): Text to write to the process's stdin,
                or None for no stdin input. Defaults to None.
            timeout (int): Maximum seconds to wait for the process.
                Defaults to 30.

        Returns:
            subprocess.CompletedProcess: Result with ``returncode``,
                ``stdout`` (str), and ``stderr`` (str) populated.
        """
        cmd = ["clawvault"] + args

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=input_data.encode() if input_data else None),
            timeout=timeout,
        )

        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=process.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )

        return result

    async def observe(
        self,
        session_path: str,
        compress: bool = True,
    ) -> List[Dict[str, Any]]:
        """Run ``clawvault observe`` on a session file.

        Args:
            session_path (str): Path to the session file to observe.
            compress (bool): Whether to pass ``--compress`` to reduce the
                observation output size. Defaults to True.

        Returns:
            List[Dict[str, Any]]: Parsed list of observation dicts, or an
                empty list on error or unexpected output.
        """
        args = ["observe"]
        if compress:
            args.append("--compress")
        args.append(session_path)

        result = await self._run_command(args)

        if result.returncode != 0:
            logger.error(f"clawvault observe failed: {result.stderr}")
            return []

        try:
            observations = json.loads(result.stdout)
            return observations if isinstance(observations, list) else []
        except json.JSONDecodeError:
            logger.error(f"Failed to parse clawvault observe output: {result.stdout}")
            return []

    async def search(
        self,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search the vault using ClawVault's built-in vector search.

        Runs ``clawvault vsearch <query> --limit <limit>``.

        Args:
            query (str): Natural-language search query.
            limit (int): Maximum number of results to return. Defaults to 10.

        Returns:
            List[Dict[str, Any]]: Parsed list of search result dicts, or an
                empty list on error or unexpected output.
        """
        args = ["vsearch", query, "--limit", str(limit)]

        result = await self._run_command(args)

        if result.returncode != 0:
            logger.error(f"clawvault vsearch failed: {result.stderr}")
            return []

        try:
            results = json.loads(result.stdout)
            return results if isinstance(results, list) else []
        except json.JSONDecodeError:
            logger.error(f"Failed to parse clawvault vsearch output: {result.stdout}")
            return []

    async def wake(self) -> Dict[str, Any]:
        """Run ``clawvault wake`` to restore persisted context.

        Returns:
            Dict[str, Any]: Parsed wake context dict, or an empty dict on
                error or unexpected output.
        """
        result = await self._run_command(["wake"])

        if result.returncode != 0:
            logger.error(f"clawvault wake failed: {result.stderr}")
            return {}

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse clawvault wake output: {result.stdout}")
            return {}

    async def checkpoint(
        self,
        session_path: str,
    ) -> str:
        """Create a checkpoint for a session via ``clawvault checkpoint``.

        Args:
            session_path (str): Path to the session file for which a
                checkpoint should be created.

        Returns:
            str: The checkpoint ID returned by clawvault, or an empty string
                on error.
        """
        result = await self._run_command(["checkpoint", session_path])

        if result.returncode != 0:
            logger.error(f"clawvault checkpoint failed: {result.stderr}")
            return ""

        return result.stdout.strip()

    async def graph(self) -> Dict[str, Any]:
        """Retrieve the memory graph via ``clawvault graph``.

        Returns:
            Dict[str, Any]: Parsed graph data dict where keys are memory
                entry identifiers, or an empty dict on error.
        """
        result = await self._run_command(["graph"])

        if result.returncode != 0:
            logger.error(f"clawvault graph failed: {result.stderr}")
            return {}

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse clawvault graph output: {result.stdout}")
            return {}

    async def store(
        self,
        data: Dict[str, Any],
    ) -> bool:
        """Store data in the vault via ``clawvault store --stdin``.

        Serialises *data* as JSON and writes it to the subprocess's stdin.

        Args:
            data (Dict[str, Any]): Data dict to persist in the vault.

        Returns:
            bool: True if the command succeeded, False otherwise.
        """
        result = await self._run_command(
            ["store", "--stdin"],
            input_data=json.dumps(data),
        )

        if result.returncode != 0:
            logger.error(f"clawvault store failed: {result.stderr}")
            return False

        return True

    async def recall(
        self,
        session_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Recall memories for a specific session via ``clawvault recall``.

        Args:
            session_id (str): Session ID (or memory key) to recall.
            limit (int): Maximum number of results to return. Defaults to 10.

        Returns:
            List[Dict[str, Any]]: Parsed list of memory dicts, or an empty
                list on error or unexpected output.
        """
        args = ["recall", session_id, "--limit", str(limit)]

        result = await self._run_command(args)

        if result.returncode != 0:
            logger.error(f"clawvault recall failed: {result.stderr}")
            return []

        try:
            memories = json.loads(result.stdout)
            return memories if isinstance(memories, list) else []
        except json.JSONDecodeError:
            logger.error(f"Failed to parse clawvault recall output: {result.stdout}")
            return []

    async def sync(self) -> bool:
        """Sync the vault with its remote via ``clawvault sync``.

        Returns:
            bool: True if the sync command succeeded, False otherwise.
        """
        result = await self._run_command(["sync"])

        if result.returncode != 0:
            logger.error(f"clawvault sync failed: {result.stderr}")
            return False

        return True
