"""Dynamic plugin loader for pyclaw."""

import importlib.util
import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from pyclaw.plugins import Plugin, PluginMetadata, PluginType


logger = logging.getLogger("pyclaw.plugins")


class PluginLoader:
    """
    Dynamic plugin loader.
    
    Discovers and loads plugins from:
    - Built-in plugins directory (pyclaw/plugins/channels/)
    - External plugin directories
    - External Python packages
    """
    
    def __init__(self, plugin_dirs: Optional[List[Path]] = None):
        """
        Initialize the plugin loader.
        
        Args:
            plugin_dirs: Additional directories to scan for plugins
        """
        self.plugin_dirs = plugin_dirs or []
        
        # Default to built-in channels directory
        self._builtin_dir = Path(__file__).parent / "channels"
        if self._builtin_dir.exists():
            self.plugin_dirs.insert(0, self._builtin_dir)
    
    def discover_plugins(self) -> Dict[str, Path]:
        """
        Find all plugins in plugin directories.
        
        Looks for:
        - Directories with plugin.py file
        - Python packages with entry point
        
        Returns:
            Dict mapping plugin name to path
        """
        plugins = {}
        
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                logger.debug(f"Plugin directory not found: {plugin_dir}")
                continue
            
            logger.debug(f"Scanning plugin directory: {plugin_dir}")
            
            for entry in plugin_dir.iterdir():
                if not entry.is_dir():
                    continue
                
                # Check for plugin.py
                plugin_file = entry / "plugin.py"
                if plugin_file.exists():
                    plugins[entry.name] = entry
                    logger.debug(f"Found plugin: {entry.name} at {entry}")
                    continue
                
                # Check for package with __init__.py
                init_file = entry / "__init__.py"
                if init_file.exists():
                    # Check if it's a valid plugin package
                    if self._is_plugin_package(entry):
                        plugins[entry.name] = entry
                        logger.debug(f"Found plugin package: {entry.name}")
        
        logger.info(f"Discovered {len(plugins)} plugins: {list(plugins.keys())}")
        return plugins
    
    def _is_plugin_package(self, path: Path) -> bool:
        """Check if a directory is a plugin package."""
        init_file = path / "__init__.py"
        if not init_file.exists():
            return False
        
        try:
            spec = importlib.util.spec_from_file_location("temp", init_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                # Check for Plugin subclass
                return hasattr(module, 'Plugin') or hasattr(module, 'ChannelPlugin')
        except Exception as e:
            logger.warning(f"Failed to check package {path.name}: {e}")
        
        return False
    
    def load_plugin_from_file(
        self,
        name: str,
        path: Path,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Plugin]:
        """
        Load a plugin from a plugin.py file.
        
        Args:
            name: Plugin name
            path: Path to plugin directory
            config: Plugin configuration
            
        Returns:
            Loaded plugin instance or None
        """
        plugin_file = path / "plugin.py"
        if not plugin_file.exists():
            logger.error(f"Plugin file not found: {plugin_file}")
            return None
        
        try:
            # Create unique module name
            module_name = f"pyclaw.dynamic_plugins.{name}"
            
            # Load the module
            spec = importlib.util.spec_from_file_location(
                module_name,
                plugin_file
            )
            if spec is None or spec.loader is None:
                logger.error(f"Failed to create module spec for {name}")
                return None
            
            module = importlib.util.module_from_spec(spec)
            
            # Add config to module for plugin to access
            module.PLUGIN_CONFIG = config or {}
            
            spec.loader.exec_module(module)
            
            # Find Plugin class in module
            plugin_class = self._find_plugin_class(module)
            if plugin_class is None:
                logger.error(f"No Plugin class found in {name}")
                return None
            
            # Instantiate plugin
            instance = plugin_class(config or {})
            logger.info(f"Loaded plugin: {name} ({instance.version})")
            return instance
            
        except Exception as e:
            logger.error(f"Failed to load plugin {name}: {e}")
            return None
    
    def load_plugin_from_module(
        self,
        module_name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Plugin]:
        """
        Load a plugin from an installed Python module.
        
        Args:
            module_name: Python module name
            config: Plugin configuration
            
        Returns:
            Loaded plugin instance or None
        """
        try:
            module = importlib.import_module(module_name)
            
            # Find Plugin class
            plugin_class = self._find_plugin_class(module)
            if plugin_class is None:
                logger.error(f"No Plugin class found in {module_name}")
                return None
            
            instance = plugin_class(config or {})
            logger.info(f"Loaded plugin from module: {module_name}")
            return instance
            
        except ImportError as e:
            logger.error(f"Failed to import plugin module {module_name}: {e}")
            return None
    
    def _find_plugin_class(self, module: Any) -> Optional[Type[Plugin]]:
        """Find Plugin or ChannelPlugin class in a module."""
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            
            if isinstance(attr, type):
                # Check if it's a subclass of Plugin but not Plugin itself
                if (attr is not Plugin and 
                    issubclass(attr, Plugin)):
                    return attr
        
        return None
    
    def get_plugin_metadata(
        self,
        plugin_class: Type[Plugin],
    ) -> PluginMetadata:
        """
        Get metadata from a plugin class.
        
        Args:
            plugin_class: Plugin class
            
        Returns:
            Plugin metadata
        """
        if hasattr(plugin_class, 'metadata'):
            return plugin_class.metadata
        
        # Create default metadata from class name
        return PluginMetadata(
            name=plugin_class.__name__,
            version="0.0.0",
            description=plugin_class.__doc__ or "",
            plugin_type=PluginType.CHANNEL,  # Default
        )


class BuiltinPluginLoader:
    """
    Loads built-in channel plugins.
    
    These are channels bundled with pyclaw.
    """
    
    CHANNEL_PLUGINS = {
        "telegram": "pyclaw.plugins.channels.telegram",
        "discord": "pyclaw.plugins.channels.discord",
    }
    
    def __init__(self, registry):
        """
        Initialize with a plugin registry.
        
        Args:
            registry: PluginRegistry instance
        """
        self.registry = registry
    
    def register_builtin_plugins(self) -> None:
        """Register all built-in plugins."""
        for name, module_name in self.CHANNEL_PLUGINS.items():
            try:
                module = importlib.import_module(module_name)
                plugin_class = self._find_plugin_class(module)
                
                if plugin_class:
                    self.registry.register_plugin_class(
                        name,
                        plugin_class,
                    )
                    logger.info(f"Registered builtin plugin: {name}")
            except ImportError as e:
                logger.warning(f"Failed to import builtin plugin {name}: {e}")
    
    def _find_plugin_class(self, module: Any) -> Optional[Type[Plugin]]:
        """Find Plugin class in module."""
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                return attr
        return None


__all__ = ["PluginLoader", "BuiltinPluginLoader"]
