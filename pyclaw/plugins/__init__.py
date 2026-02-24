"""Plugin system for pyclaw."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyclaw.core.gateway import Gateway


class PluginType(str, Enum):
    """Types of plugins supported by pyclaw."""
    CHANNEL = "channel"
    SKILL = "skill"
    TOOL = "tool"
    MIDDLEWARE = "middleware"
    STORAGE = "storage"


class PluginState(str, Enum):
    """Lifecycle states of a plugin."""
    DISCOVERED = "discovered"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class PluginMetadata:
    """Metadata for a plugin."""
    name: str
    version: str
    description: str = ""
    author: str = ""
    plugin_type: PluginType = PluginType.CHANNEL
    dependencies: List[str] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if isinstance(self.plugin_type, str):
            self.plugin_type = PluginType(self.plugin_type)


@dataclass
class PluginInfo:
    """Complete information about a loaded plugin."""
    metadata: PluginMetadata
    instance: Optional['Plugin'] = None
    state: PluginState = PluginState.DISCOVERED
    error: Optional[str] = None
    loaded_at: Optional[datetime] = None
    config: Dict[str, Any] = field(default_factory=dict)


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
    def metadata(self) -> PluginMetadata:
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


__all__ = [
    "Plugin",
    "PluginType",
    "PluginState",
    "PluginMetadata",
    "PluginInfo",
    "ChannelPlugin",
]
