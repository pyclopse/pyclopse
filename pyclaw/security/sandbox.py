"""Command sandboxing for pyclaw."""

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import logging

from pyclaw.config.schema import SandboxConfig, DockerSandboxConfig


@dataclass
class ExecutionResult:
    """Result of sandboxed command execution."""
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    error: Optional[str] = None


class Sandbox(ABC):
    """Abstract base class for command sandboxes."""
    
    @abstractmethod
    async def execute(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute a command in the sandbox."""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the sandbox is available."""
        pass


class NoSandbox(Sandbox):
    """No sandbox - commands run directly (development only!)."""
    
    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._logger = logging.getLogger("pyclaw.sandbox")
    
    async def execute(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute command directly without sandboxing."""
        import time
        start_time = time.time()
        
        # Merge environment
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=exec_env,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ExecutionResult(
                    success=False,
                    error=f"Command timed out after {timeout}s",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
            
            duration_ms = int((time.time() - start_time) * 1000)
            
            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode or 0,
                duration_ms=duration_ms,
            )
            
        except Exception as e:
            self._logger.error(f"Sandbox execution error: {e}")
            return ExecutionResult(
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )
    
    async def is_available(self) -> bool:
        """NoSandbox is always available."""
        return True


class DockerSandbox(Sandbox):
    """Docker-based sandbox for command isolation."""
    
    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        docker_config: Optional[DockerSandboxConfig] = None,
    ):
        self.config = config or SandboxConfig()
        self.docker_config = docker_config or DockerSandboxConfig()
        self._logger = logging.getLogger("pyclaw.sandbox.docker")
    
    async def is_available(self) -> bool:
        """Check if Docker is available."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0
        except FileNotFoundError:
            return False
    
    async def execute(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute command in Docker container."""
        import time
        start_time = time.time()
        
        if not await self.is_available():
            return ExecutionResult(
                success=False,
                error="Docker is not available",
            )
        
        # Build docker run command
        cmd = [
            "docker", "run",
            "--rm",
            "--network", self.docker_config.network,
            "-v", f"{cwd}:{cwd}",
            "-w", cwd,
        ]
        
        # Add resource limits if configured
        if self.docker_config.memory_limit:
            cmd.extend(["--memory", self.docker_config.memory_limit])
        
        if self.docker_config.cpu_limit:
            cmd.extend(["--cpus", str(self.docker_config.cpu_limit)])
        
        if self.docker_config.pids_limit:
            cmd.extend(["--pids-limit", str(self.docker_config.pids_limit)])
        
        # Read-only root filesystem
        if self.docker_config.read_only:
            cmd.append("--read-only")
        
        # Tmpfs for /tmp if configured
        if self.docker_config.tmp_size:
            cmd.extend(["--tmpfs", f"/tmp:size={self.docker_config.tmp_size}m"])
        
        # Add allowed volumes
        for volume in self.docker_config.allowed_volumes:
            cmd.extend(["-v", volume])
        
        # Add environment variables
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        for key, value in exec_env.items():
            cmd.extend(["-e", f"{key}={value}"])
        
        # Add image
        cmd.append(self.docker_config.image)
        
        # Add command
        cmd.append("sh")
        cmd.append("-c")
        cmd.append(command)
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ExecutionResult(
                    success=False,
                    error=f"Command timed out after {timeout}s",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
            
            duration_ms = int((time.time() - start_time) * 1000)
            
            return ExecutionResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode or 0,
                duration_ms=duration_ms,
            )
            
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                error="Docker not found",
                duration_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            self._logger.error(f"Docker sandbox execution error: {e}")
            return ExecutionResult(
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )


def create_sandbox(config: SandboxConfig) -> Sandbox:
    """Factory function to create appropriate sandbox."""
    if not config.enabled:
        return NoSandbox(config)
    
    if config.type == "docker":
        return DockerSandbox(config, config.docker)
    
    # Default to no sandbox
    return NoSandbox(config)


class DockerContainerManager:
    """Manage Docker containers for sandboxed execution."""
    
    def __init__(self, docker_config: Optional[DockerSandboxConfig] = None):
        self.docker_config = docker_config or DockerSandboxConfig()
        self._logger = logging.getLogger("pyclaw.sandbox.docker.manager")
    
    async def is_docker_available(self) -> bool:
        """Check if Docker is available."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0
        except FileNotFoundError:
            return False
    
    async def pull_image(self) -> bool:
        """Pull the configured Docker image."""
        if not await self.is_docker_available():
            return False
        
        self._logger.info(f"Pulling Docker image: {self.docker_config.image}")
        process = await asyncio.create_subprocess_exec(
            "docker", "pull", self.docker_config.image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            self._logger.error(f"Failed to pull image: {stderr.decode()}")
            return False
        
        return True
    
    async def list_containers(self) -> List[Dict[str, Any]]:
        """List running containers."""
        if not await self.is_docker_available():
            return []
        
        process = await asyncio.create_subprocess_exec(
            "docker", "ps", "--format", "{{json . }}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            return []
        
        containers = []
        for line in stdout.decode().strip().split("\n"):
            if line:
                import json
                containers.append(json.loads(line))
        return containers
    
    async def prune_containers(self) -> bool:
        """Prune stopped containers."""
        if not await self.is_docker_available():
            return False
        
        process = await asyncio.create_subprocess_exec(
            "docker", "container", "prune", "-f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return process.returncode == 0
