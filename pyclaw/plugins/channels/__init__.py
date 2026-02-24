"""Channel plugins for pyclaw."""

from typing import Any, Dict, Optional

from pyclaw.plugins import ChannelPlugin, PluginMetadata, PluginType


class BaseChannelPlugin(ChannelPlugin):
    """
    Base class for channel plugins with common functionality.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._message_handler = None
    
    @property
    def channel_name(self) -> str:
        """Return the channel name. Override in subclass."""
        raise NotImplementedError
    
    def set_message_handler(self, handler) -> None:
        """Set the message handler callback."""
        self._message_handler = handler
    
    async def handle_webhook(self, channel: str, data: dict) -> Optional[dict]:
        """Handle incoming webhook - override in subclass."""
        raise NotImplementedError
    
    async def send_message(self, target: Dict[str, Any], content: str) -> str:
        """Send a message - override in subclass."""
        raise NotImplementedError


__all__ = ["BaseChannelPlugin"]
