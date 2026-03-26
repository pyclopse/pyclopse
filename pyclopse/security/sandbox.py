"""Command sandboxing for pyclopse."""

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import logging

from pyclopse.config.schema import SandboxConfig, DockerSandboxConfig


@dataclass
class ExecutionResult:
    """Result of a sandboxed command execution.

    Attributes:
        success (bool): True if the command exited with code 0.
        stdout (str): Decoded standard output of the command.
        stderr (str): Decoded standard error of the command.
        exit_code (int): Process exit code. Defaults to 0.
        duration_ms (int): Wall-clock execution time in milliseconds.
        error (Optional[str]): High-level error description for system-level
            failures (e.g. timeout, Docker unavailable), distinct from
            stderr.  None when not applicable.
    """

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    error: Optional[str] = None


class Sandbox(ABC):
    """Abstract base class for command execution sandboxes.

    Subclasses implement different isolation strategies (no sandbox,
    Docker, etc.).  Use :func:`create_sandbox` to obtain the correct
    implementation based on the config.
    """

    @abstractmethod
    async def execute(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute a command in this sandbox.

        Implementors must run *command* inside their isolation boundary,
        capturing stdout and stderr, and return an :class:`ExecutionResult`.

        Args:
            command (str): Shell command string to execute.
            cwd (str): Working directory for the command.
            env (Optional[Dict[str, str]]): Extra environment variables to
                merge into the execution environment. Defaults to None.
            timeout (Optional[int]): Maximum execution time in seconds.
                None means no timeout. Defaults to None.

        Returns:
            ExecutionResult: Outcome of the command including stdout,
                stderr, exit code, and wall-clock duration.
        """
        pass

    @abstractmethod
    async def is_available(self) -> bool:
        """Check whether this sandbox implementation is usable.

        Implementors should verify that all external dependencies
        (e.g. the Docker daemon) are accessible.

        Returns:
            bool: True if the sandbox is ready to accept commands.
        """
        pass


class NoSandbox(Sandbox):
    """Unsandboxed execution — commands run directly in the host process.

    Intended for development and testing only.  In production, use
    :class:`DockerSandbox` for isolation.

    Attributes:
        config (SandboxConfig): Sandbox configuration object.
        _logger (logging.Logger): Logger for execution errors.
    """

    def __init__(self, config: Optional[SandboxConfig] = None) -> None:
        """Initialise the no-op sandbox.

        Args:
            config (Optional[SandboxConfig]): Sandbox configuration.
                Defaults to a default-constructed :class:`SandboxConfig`.
        """
        self.config = config or SandboxConfig()
        self._logger = logging.getLogger("pyclopse.sandbox")

    async def execute(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute *command* directly in the host shell without sandboxing.

        Merges *env* into a copy of the current process environment and
        runs the command via ``asyncio.create_subprocess_shell``.  Kills
        the process and returns a timeout result if it exceeds *timeout*.

        Args:
            command (str): Shell command string to execute.
            cwd (str): Working directory for the command.
            env (Optional[Dict[str, str]]): Additional environment variables.
                Defaults to None.
            timeout (Optional[int]): Seconds before the process is killed.
                Defaults to None (no timeout).

        Returns:
            ExecutionResult: Result with captured stdout/stderr and timing.
        """
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
        """Check whether this sandbox is available.

        :class:`NoSandbox` requires no external dependencies and is always
        available.

        Returns:
            bool: Always True.
        """
        return True


class DockerSandbox(Sandbox):
    """Docker-based sandbox that isolates command execution in a container.

    Builds a ``docker run --rm`` command with optional resource limits
    (memory, CPU, PIDs), read-only filesystem, tmpfs ``/tmp``, and
    volume mounts, then runs the user command via ``sh -c``.

    Attributes:
        config (SandboxConfig): General sandbox configuration.
        docker_config (DockerSandboxConfig): Docker-specific options
            (image, network, resource limits, volumes, etc.).
        _logger (logging.Logger): Logger for Docker execution errors.
    """

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        docker_config: Optional[DockerSandboxConfig] = None,
    ) -> None:
        """Initialise the Docker sandbox.

        Args:
            config (Optional[SandboxConfig]): General sandbox configuration.
                Defaults to a default-constructed :class:`SandboxConfig`.
            docker_config (Optional[DockerSandboxConfig]): Docker-specific
                configuration.  Defaults to a default-constructed
                :class:`DockerSandboxConfig`.
        """
        self.config = config or SandboxConfig()
        self.docker_config = docker_config or DockerSandboxConfig()
        self._logger = logging.getLogger("pyclopse.sandbox.docker")

    async def is_available(self) -> bool:
        """Check whether the Docker daemon is accessible.

        Runs ``docker --version`` and returns True only on exit code 0.

        Returns:
            bool: True if Docker is installed and responding.
        """
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
        """Execute *command* inside an isolated Docker container.

        Constructs a ``docker run --rm`` invocation with the configured
        image, resource limits, volume mounts, and environment variables,
        then runs *command* via ``sh -c``.  Returns immediately with an
        error result if Docker is unavailable.

        Args:
            command (str): Shell command string to execute inside the container.
            cwd (str): Working directory mounted into and used inside the
                container.
            env (Optional[Dict[str, str]]): Additional environment variables
                to inject.  Defaults to None.
            timeout (Optional[int]): Seconds before the container process is
                killed.  Defaults to None (no timeout).

        Returns:
            ExecutionResult: Result with captured stdout/stderr and timing.
        """
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
    """Factory function to create the appropriate :class:`Sandbox` instance.

    Selects the sandbox type based on ``config.enabled`` and ``config.type``.
    Falls back to :class:`NoSandbox` when sandboxing is disabled or the
    type is unrecognised.

    Args:
        config (SandboxConfig): Sandbox configuration from the pyclopse config
            file.

    Returns:
        Sandbox: A configured :class:`NoSandbox` or :class:`DockerSandbox`
            instance ready to accept :meth:`~Sandbox.execute` calls.
    """
    if not config.enabled:
        return NoSandbox(config)

    if config.type == "docker":
        return DockerSandbox(config, config.docker)

    # Default to no sandbox
    return NoSandbox(config)


class DockerContainerManager:
    """Utility class for managing Docker containers used in sandboxed execution.

    Provides higher-level operations (pull image, list running containers,
    prune stopped containers) that complement :class:`DockerSandbox`.

    Attributes:
        docker_config (DockerSandboxConfig): Docker-specific configuration
            including the target image name.
        _logger (logging.Logger): Logger for Docker management operations.
    """

    def __init__(self, docker_config: Optional[DockerSandboxConfig] = None) -> None:
        """Initialise the container manager.

        Args:
            docker_config (Optional[DockerSandboxConfig]): Docker-specific
                configuration.  Defaults to a default-constructed
                :class:`DockerSandboxConfig`.
        """
        self.docker_config = docker_config or DockerSandboxConfig()
        self._logger = logging.getLogger("pyclopse.sandbox.docker.manager")

    async def is_docker_available(self) -> bool:
        """Check whether the Docker CLI is accessible.

        Runs ``docker --version`` and returns True only on exit code 0.

        Returns:
            bool: True if Docker is installed and the daemon is responding.
        """
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
        """Pull the configured Docker image from its registry.

        Runs ``docker pull <image>`` and returns whether it succeeded.
        Returns False immediately if Docker is not available.

        Returns:
            bool: True if the image was pulled successfully.
        """
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
        """List currently running Docker containers.

        Runs ``docker ps --format '{{json .}}'`` and parses each JSON line.
        Returns an empty list if Docker is unavailable or the command fails.

        Returns:
            List[Dict[str, Any]]: One dict per running container as returned
                by ``docker ps --format '{{json .}}'``.
        """
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
        """Remove all stopped Docker containers via ``docker container prune -f``.

        Returns False immediately if Docker is not available.

        Returns:
            bool: True if the prune command exited successfully.
        """
        if not await self.is_docker_available():
            return False

        process = await asyncio.create_subprocess_exec(
            "docker", "container", "prune", "-f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return process.returncode == 0
