"""ClawVault CLI wrapper - subprocess interface to clawvault."""
import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pyclaw.memory")


class ClawVaultClient:
    """
    Wrapper around clawvault CLI (npm package).
    
    ClawVault is accessed via subprocess, not HTTP.
    """
    
    def __init__(self, vault_path: str = "~/.claw/vault"):
        """
        Args:
            vault_path: Path to the vault directory
        """
        self.vault_path = Path(vault_path).expanduser()
        self._check_installation()
    
    def _check_installation(self) -> None:
        """Check if clawvault CLI is installed."""
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
        """Run a clawvault command asynchronously."""
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
        """
        Run clawvault observe on a session file.
        
        Args:
            session_path: Path to the session file
            compress: Whether to compress observations
            
        Returns:
            List of observation dicts
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
        """
        Search the vault using vector search.
        
        Args:
            query: Search query
            limit: Maximum results to return
            
        Returns:
            List of search result dicts
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
        """
        Run clawvault wake to restore context.
        
        Returns:
            Wake context dict
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
        """
        Create a checkpoint for a session.
        
        Args:
            session_path: Path to the session file
            
        Returns:
            Checkpoint ID
        """
        result = await self._run_command(["checkpoint", session_path])
        
        if result.returncode != 0:
            logger.error(f"clawvault checkpoint failed: {result.stderr}")
            return ""
        
        return result.stdout.strip()
    
    async def graph(self) -> Dict[str, Any]:
        """
        Get the memory graph.
        
        Returns:
            Graph data dict
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
        """
        Store data in the vault.
        
        Args:
            data: Data to store
            
        Returns:
            True if successful
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
        """
        Recall memories for a specific session.
        
        Args:
            session_id: Session ID to recall
            limit: Maximum results
            
        Returns:
            List of memory dicts
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
        """
        Sync the vault with remote.
        
        Returns:
            True if successful
        """
        result = await self._run_command(["sync"])
        
        if result.returncode != 0:
            logger.error(f"clawvault sync failed: {result.stderr}")
            return False
        
        return True
