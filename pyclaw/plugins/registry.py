"""Plugin registry for managing loaded plugins."""

import logging
from typing import Any, Dict, List, Optional, Type

from pyclaw.plugins import (
    Plugin,
    PluginInfo,
    PluginMetadata,
    PluginState,
    PluginType,
)


logger = logging.getLogger("pyclaw.plugins")


class PluginRegistry:
    """
    Registry for managing plugins.
    
    Handles plugin registration, enabling/disabling, and retrieval.
    """
    
    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}
        self._plugin_classes: Dict[str, Type[Plugin]] = {}
        self._gateway = None
    
    @property
    def gateway(self):
        """Get the gateway instance."""
        return self._gateway
    
    @gateway.setter
    def gateway(self, value):
        """Set the gateway instance."""
        self._gateway = value
    
    def register_plugin_class(
        self,
        name: str,
        plugin_class: Type[Plugin],
        metadata: Optional[PluginMetadata] = None,
    ) -> None:
        """
        Register a plugin class.
        
        Args:
            name: Plugin name
            plugin_class: Plugin class
            metadata: Optional metadata (will be extracted from class if not provided)
        """
        if metadata is None:
            # Try to get from class attribute first
            if hasattr(plugin_class, '_metadata'):
                metadata = plugin_class._metadata
            else:
                # Create a temporary instance to get metadata
                temp_instance = plugin_class({})
                metadata = temp_instance.metadata
        
        self._plugin_classes[name] = plugin_class
        self._plugins[name] = PluginInfo(
            metadata=metadata,
            state=PluginState.DISCOVERED,
        )
        logger.info(f"Registered plugin class: {name} ({metadata.version})")
    
    def register_plugin_instance(
        self,
        name: str,
        instance: Plugin,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Register a plugin instance.
        
        Args:
            name: Plugin name
            instance: Plugin instance
            config: Plugin configuration
        """
        info = self._plugins.get(name)
        if info is None:
            info = PluginInfo(metadata=instance.metadata)
            self._plugins[name] = info
        
        info.instance = instance
        info.config = config or {}
        info.state = PluginState.LOADED
        info.loaded_at = None  # Will be set on enable
        
        logger.info(f"Loaded plugin instance: {name}")
    
    async def enable_plugin(self, name: str) -> bool:
        """
        Enable a plugin.
        
        Args:
            name: Plugin name
            
        Returns:
            True if successful
        """
        info = self._plugins.get(name)
        if info is None:
            logger.error(f"Plugin not found: {name}")
            return False
        
        if info.state == PluginState.ENABLED:
            logger.warning(f"Plugin already enabled: {name}")
            return True
        
        try:
            # Load instance if not already loaded
            if info.instance is None:
                plugin_class = self._plugin_classes.get(name)
                if plugin_class is None:
                    logger.error(f"Plugin class not registered: {name}")
                    return False
                
                instance = plugin_class(info.config)
                info.instance = instance
            
            # Call on_load
            if self._gateway:
                await info.instance.on_load(self._gateway)
            
            # Call on_enable
            await info.instance.on_enable()
            
            info.state = PluginState.ENABLED
            from datetime import datetime
            info.loaded_at = datetime.now()
            info.error = None
            
            logger.info(f"Enabled plugin: {name}")
            return True
            
        except Exception as e:
            info.state = PluginState.ERROR
            info.error = str(e)
            logger.error(f"Failed to enable plugin {name}: {e}")
            return False
    
    async def disable_plugin(self, name: str) -> bool:
        """
        Disable a plugin.
        
        Args:
            name: Plugin name
            
        Returns:
            True if successful
        """
        info = self._plugins.get(name)
        if info is None:
            logger.error(f"Plugin not found: {name}")
            return False
        
        if info.state != PluginState.ENABLED:
            logger.warning(f"Plugin not enabled: {name}")
            return True
        
        try:
            # Call on_disable
            if info.instance:
                await info.instance.on_disable()
                await info.instance.on_unload()
            
            info.state = PluginState.DISABLED
            logger.info(f"Disabled plugin: {name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to disable plugin {name}: {e}")
            return False
    
    def get_plugin(self, name: str) -> Optional[Plugin]:
        """
        Get a plugin instance by name.
        
        Args:
            name: Plugin name
            
        Returns:
            Plugin instance or None
        """
        info = self._plugins.get(name)
        return info.instance if info else None
    
    def get_plugin_info(self, name: str) -> Optional[PluginInfo]:
        """
        Get plugin info by name.
        
        Args:
            name: Plugin name
            
        Returns:
            Plugin info or None
        """
        return self._plugins.get(name)
    
    def list_plugins(
        self,
        plugin_type: Optional[PluginType] = None,
        state: Optional[PluginState] = None,
    ) -> List[PluginInfo]:
        """
        List plugins with optional filtering.
        
        Args:
            plugin_type: Filter by plugin type
            state: Filter by state
            
        Returns:
            List of matching plugin infos
        """
        results = []
        for info in self._plugins.values():
            if plugin_type and info.metadata.plugin_type != plugin_type:
                continue
            if state and info.state != state:
                continue
            results.append(info)
        return results
    
    def list_enabled(self) -> List[Plugin]:
        """
        Get all enabled plugin instances.
        
        Returns:
            List of enabled plugins
        """
        enabled = []
        for info in self._plugins.values():
            if info.state == PluginState.ENABLED and info.instance:
                enabled.append(info.instance)
        return enabled
    
    def list_enabled_channels(self) -> List[Plugin]:
        """
        Get all enabled channel plugins.
        
        Returns:
            List of enabled channel plugins
        """
        channels = []
        for info in self._plugins.values():
            if (info.state == PluginState.ENABLED and 
                info.instance and 
                info.metadata.plugin_type == PluginType.CHANNEL):
                channels.append(info.instance)
        return channels
    
    async def unload_all(self) -> None:
        """Unload all plugins."""
        for name in list(self._plugins.keys()):
            if self._plugins[name].state == PluginState.ENABLED:
                await self.disable_plugin(name)
        
        self._plugins.clear()
        self._plugin_classes.clear()
        logger.info("Unloaded all plugins")


__all__ = ["PluginRegistry"]
