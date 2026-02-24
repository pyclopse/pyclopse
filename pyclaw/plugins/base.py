"""Plugin base classes."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyclaw.core.gateway import Gateway


class Plugin(ABC):
    """
    Abstract base class for all pyclaw plugins.
    
    Plugins extend pyclaw's functionality by adding:
    - Channel adapters (Telegram, Discord, etc.)
    - Skills and tools
    - Middleware for request/response processing
    - Storage backends
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the plugin.
        
        Args:
            config: Plugin configuration dict
        """
        self.config = config
        self._gateway: Optional['Gateway'] = None
        self._enabled = False
    
    @property
    @abstractmethod
    def metadata(self) -> 'PluginMetadata':
        """Return plugin metadata."""
        pass
    
    @property
    def name(self) -> str:
        """Return plugin name."""
        return self.metadata.name
    
    @property
    def version(self) -> str:
        """Return plugin version."""
        return self.metadata.version
    
    @property
    def is_enabled(self) -> bool:
        """Check if plugin is enabled."""
        return self._enabled
    
    async def on_load(self, gateway: 'Gateway') -> None:
        """
        Called when the plugin is loaded.
        
        Args:
            gateway: The gateway instance
        """
        self._gateway = gateway
        self._enabled = True
    
    async def on_unload(self) -> None:
        """
        Called when the plugin is unloaded or disabled.
        """
        self._enabled = False
        self._gateway = None
    
    async def on_enable(self) -> None:
        """Called when the plugin is enabled."""
        pass
    
    async def on_disable(self) -> None:
        """Called when the plugin is disabled."""
        pass
    
    async def handle_webhook(self, channel: str, data: dict) -> Optional[dict]:
        """
        Handle incoming webhook data.
        
        Args:
            channel: Channel name (e.g., 'telegram', 'discord')
            data: Webhook payload
            
        Returns:
            Processed data or None
        """
        return data
    
    def get_routes(self) -> List[Dict[str, Any]]:
        """
        Get additional routes to add to the API.
        
        Returns:
            List of route definitions
        """
        return []


class ChannelPlugin(Plugin):
    """
    Base class for channel adapter plugins.
    
    Channels are messaging platforms like Telegram, Discord, Slack, etc.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._channel_adapter = None
    
    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return the channel name (e.g., 'telegram', 'discord')."""
        pass
    
    @property
    def channel_adapter(self):
        """Return the channel adapter instance."""
        return self._channel_adapter
    
    async def connect(self) -> None:
        """Connect to the channel."""
        pass
    
    async def disconnect(self) -> None:
        """Disconnect from the channel."""
        pass
    
    async def send_message(self, target: Dict[str, Any], content: str) -> str:
        """
        Send a message to the channel.
        
        Args:
            target: Message target dict
            content: Message content
            
        Returns:
            Message ID
        """
        raise NotImplementedError
    
    async def handle_webhook(self, channel: str, data: dict) -> Optional[dict]:
        """Handle incoming webhook and convert to internal message format."""
        return data


# Import for type hints (avoid circular import)
from pyclaw.plugins.types import PluginMetadata


__all__ = ["Plugin", "ChannelPlugin", "PluginMetadata"]
