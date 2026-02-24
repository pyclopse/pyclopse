"""Multi-type plugin loader for pyclaw.

Supports loading plugins of different types:
- python: Load Python class directly
- http: HTTP/RPC plugin (any language)
- subprocess: stdio communication (any language)
- json: Config-only plugins (no code)
"""

import asyncio
import importlib.util
import json
import logging
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import httpx

from pyclaw.plugins import Plugin, PluginMetadata, PluginType

logger = logging.getLogger("pyclaw.plugins")


# ============================================================================
# Plugin Loader Interface
# ============================================================================

class PluginLoaderBase(ABC):
    """Base class for plugin loaders."""

    @abstractmethod
    async def load(self, name: str, config: Dict[str, Any]) -> Optional[Plugin]:
        """Load a plugin instance."""
        pass

    @abstractmethod
    async def health_check(self, name: str) -> bool:
        """Check if plugin is healthy."""
        pass


# ============================================================================
# Python Plugin Loader
# ============================================================================

class PythonPluginLoader(PluginLoaderBase):
    """Loads native Python plugins."""

    def __init__(self, plugin_dirs: Optional[List[Path]] = None):
        self.plugin_dirs = plugin_dirs or []
        self._builtin_dir = Path(__file__).parent / "channels"
        if self._builtin_dir.exists():
            self.plugin_dirs.insert(0, self._builtin_dir)

    async def load(self, name: str, config: Dict[str, Any]) -> Optional[Plugin]:
        """Load a Python plugin."""
        plugin_path = config.get("path")
        module_name = config.get("module")

        if plugin_path:
            return await self._load_from_path(name, Path(plugin_path), config)
        elif module_name:
            return await self._load_from_module(name, module_name, config)
        else:
            # Try to discover in plugin directories
            return await self._load_from_dirs(name, config)

    async def health_check(self, name: str) -> bool:
        """Python plugins are always healthy if loaded."""
        return True

    async def _load_from_path(
        self, name: str, path: Path, config: Dict[str, Any]
    ) -> Optional[Plugin]:
        """Load plugin from a specific path."""
        plugin_file = path / "plugin.py"
        if not plugin_file.exists():
            logger.error(f"Plugin file not found: {plugin_file}")
            return None

        try:
            module_name = f"pyclaw.dynamic_plugins.{name}"
            spec = importlib.util.spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                return None

            module = importlib.util.module_from_spec(spec)
            module.PLUGIN_CONFIG = config.get("config", {})
            spec.loader.exec_module(module)

            plugin_class = self._find_plugin_class(module)
            if plugin_class is None:
                logger.error(f"No Plugin class found in {name}")
                return None

            instance = plugin_class(config.get("config", {}))
            logger.info(f"Loaded Python plugin: {name}")
            return instance
        except Exception as e:
            logger.error(f"Failed to load Python plugin {name}: {e}")
            return None

    async def _load_from_module(
        self, name: str, module_name: str, config: Dict[str, Any]
    ) -> Optional[Plugin]:
        """Load plugin from an installed module."""
        try:
            module = importlib.import_module(module_name)
            plugin_class = self._find_plugin_class(module)
            if plugin_class is None:
                logger.error(f"No Plugin class found in {module_name}")
                return None

            instance = plugin_class(config.get("config", {}))
            logger.info(f"Loaded Python plugin from module: {name}")
            return instance
        except ImportError as e:
            logger.error(f"Failed to import plugin module {module_name}: {e}")
            return None

    async def _load_from_dirs(
        self, name: str, config: Dict[str, Any]
    ) -> Optional[Plugin]:
        """Discover and load plugin from configured directories."""
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                continue
            plugin_path = plugin_dir / name
            if plugin_path.exists() and plugin_path.is_dir():
                return await self._load_from_path(name, plugin_path, config)
        return None

    def _find_plugin_class(self, module: Any) -> Optional[Type[Plugin]]:
        """Find Plugin class in module."""
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                return attr
        return None


# ============================================================================
# HTTP Plugin Loader
# ============================================================================

class HTTPPluginLoader(PluginLoaderBase):
    """Loads HTTP/RPC plugins (any language)."""

    def __init__(self):
        self._plugins: Dict[str, Dict[str, Any]] = {}

    async def load(self, name: str, config: Dict[str, Any]) -> Optional[Plugin]:
        """Load an HTTP plugin."""
        url = config.get("url")
        if not url:
            logger.error(f"HTTP plugin {name} missing URL")
            return None

        self._plugins[name] = {
            "url": url,
            "health": config.get("health", f"{url.rstrip('/')}/health"),
            "config": config.get("config", {}),
        }

        # Verify health endpoint exists
        if not await self.health_check(name):
            logger.warning(f"HTTP plugin {name} health check failed")

        logger.info(f"Loaded HTTP plugin: {name} -> {url}")
        return HTTPlugin(name, url, config.get("health"), config.get("config", {}))

    async def health_check(self, name: str) -> bool:
        """Check HTTP plugin health."""
        plugin = self._plugins.get(name)
        if not plugin:
            return False

        health_url = plugin.get("health")
        if not health_url:
            return True

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(health_url)
                return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Health check failed for {name}: {e}")
            return False


class HTTPlugin(Plugin):
    """HTTP/RPC plugin wrapper."""

    def __init__(self, name: str, base_url: str, health_url: Optional[str], config: Dict[str, Any]):
        super().__init__(config)
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._health_url = health_url
        self._metadata = PluginMetadata(
            name=name,
            version="0.0.0",
            description=f"HTTP plugin: {base_url}",
            plugin_type=PluginType.HTTP,
        )

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def on_load(self, gateway: Any) -> None:
        await super().on_load(gateway)
        logger.info(f"HTTP plugin connected: {self._name}")

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call a remote method on the plugin."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._base_url}/{method}",
                    json=params or {},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"HTTP plugin call failed: {method}: {e}")
            return {"error": str(e)}


# ============================================================================
# Subprocess Plugin Loader
# ============================================================================

class SubprocessPluginLoader(PluginLoaderBase):
    """Loads plugins that communicate via stdin/stdout."""

    def __init__(self):
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._plugin_configs: Dict[str, Dict[str, Any]] = {}

    async def load(self, name: str, config: Dict[str, Any]) -> Optional[Plugin]:
        """Load a subprocess plugin."""
        command = config.get("command")
        if not command:
            logger.error(f"Subprocess plugin {name} missing command")
            return None

        try:
            # Start the subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self._processes[name] = process
            self._plugin_configs[name] = {
                "protocol": config.get("protocol", "json"),
                "config": config.get("config", {}),
            }

            logger.info(f"Loaded subprocess plugin: {name} -> {command}")
            return SubprocessPlugin(name, process, config.get("protocol", "json"), config.get("config", {}))
        except Exception as e:
            logger.error(f"Failed to start subprocess plugin {name}: {e}")
            return None

    async def health_check(self, name: str) -> bool:
        """Check if subprocess is still running."""
        process = self._processes.get(name)
        if not process:
            return False
        return process.returncode is None


class SubprocessPlugin(Plugin):
    """Subprocess plugin wrapper."""

    def __init__(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        protocol: str,
        config: Dict[str, Any],
    ):
        super().__init__(config)
        self._name = name
        self._process = process
        self._protocol = protocol
        self._metadata = PluginMetadata(
            name=name,
            version="0.0.0",
            description=f"Subprocess plugin: {process.args}",
            plugin_type=PluginType.SUBPROCESS,
        )

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def on_load(self, gateway: Any) -> None:
        await super().on_load(gateway)
        logger.info(f"Subprocess plugin started: {self._name}")

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a request to the subprocess."""
        if self._protocol == "json":
            request = json.dumps({"method": method, "params": params or {}})
            self._process.stdin.write((request + "\n").encode())
            await self._process.stdin.drain()

            # Read response
            line = await self._process.stdout.readline()
            if line:
                try:
                    return json.loads(line.decode())
                except json.JSONDecodeError:
                    return {"error": "Invalid JSON response"}
        else:
            # Simple text protocol
            self._process.stdin.write(f"{method}:{json.dumps(params or {})}\n".encode())
            await self._process.stdin.drain()
            line = await self._process.stdout.readline()
            if line:
                return {"response": line.decode().strip()}
        return {"error": "No response"}

    async def on_unload(self) -> None:
        """Clean up subprocess."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
        await super().on_unload()


# ============================================================================
# JSON Config Plugin Loader
# ============================================================================

class JSONPluginLoader(PluginLoaderBase):
    """Loads config-only plugins (no code)."""

    async def load(self, name: str, config: Dict[str, Any]) -> Optional[Plugin]:
        """Load a JSON/config-only plugin."""
        plugin_config = config.get("config", {})

        logger.info(f"Loaded JSON plugin: {name}")
        return JSONPlugin(name, plugin_config)

    async def health_check(self, name: str) -> bool:
        """JSON plugins are always healthy."""
        return True


class JSONPlugin(Plugin):
    """Config-only plugin (no code)."""

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(config)
        self._metadata = PluginMetadata(
            name=name,
            version="0.0.0",
            description=f"JSON config plugin: {config}",
            plugin_type=PluginType.JSON,
        )

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata


# ============================================================================
# Multi-Type Plugin Manager
# ============================================================================

class MultiTypePluginLoader:
    """Manages plugins of different types."""

    def __init__(self, plugin_dirs: Optional[List[Path]] = None):
        self._loaders = {
            PluginType.PYTHON: PythonPluginLoader(plugin_dirs),
            PluginType.HTTP: HTTPPluginLoader(),
            PluginType.SUBPROCESS: SubprocessPluginLoader(),
            PluginType.JSON: JSONPluginLoader(),
        }

    async def load_plugin(
        self, name: str, plugin_type: PluginType, config: Dict[str, Any]
    ) -> Optional[Plugin]:
        """Load a plugin by type."""
        loader = self._loaders.get(plugin_type)
        if not loader:
            logger.error(f"Unknown plugin type: {plugin_type}")
            return None

        return await loader.load(name, config)

    async def health_check(self, name: str, plugin_type: PluginType) -> bool:
        """Check plugin health by type."""
        loader = self._loaders.get(plugin_type)
        if not loader:
            return False
        return await loader.health_check(name)


__all__ = [
    "PluginLoaderBase",
    "PythonPluginLoader",
    "HTTPPluginLoader",
    "SubprocessPluginLoader",
    "JSONPluginLoader",
    "MultiTypePluginLoader",
    "HTTPlugin",
    "SubprocessPlugin",
    "JSONPlugin",
]
