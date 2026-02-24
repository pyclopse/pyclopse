"""Discord channel plugin."""

import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime

from pyclaw.plugins import ChannelPlugin, PluginMetadata, PluginType


logger = logging.getLogger("pyclaw.plugins.discord")


class DiscordPlugin(ChannelPlugin):
    """Discord bot plugin using discord.py."""
    
    VERSION = "1.0.0"
    
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="discord",
            version=self.VERSION,
            description="Discord messaging channel plugin",
            author="pyclaw",
            plugin_type=PluginType.CHANNEL,
            tags=["messaging", "discord"],
        )
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.token = config.get("bot_token")
        self._client = None
        self._message_handler = None
        self._running = False
    
    @property
    def channel_name(self) -> str:
        return "discord"
    
    @property
    def is_connected(self) -> bool:
        return self._running and self._client is not None
    
    async def on_load(self, gateway) -> None:
        """Initialize the Discord bot."""
        await super().on_load(gateway)
        await self.connect()
    
    async def on_unload(self) -> None:
        """Cleanup on unload."""
        await self.disconnect()
        await super().on_unload()
    
    async def connect(self) -> None:
        """Initialize and start the Discord bot."""
        try:
            import discord
            from discord import DiscordException
            
            intents = discord.Intents.default()
            intents.message_content = True
            
            class DiscordClient(discord.Client):
                async def on_ready(self):
                    logger.info(f"Logged in as {self.user}")
                
                async def on_message(self, message):
                    # Ignore bot messages
                    if message.author == self.user:
                        return
                    
                    # Build internal message format
                    msg_data = {
                        "id": str(message.id),
                        "channel": "discord",
                        "sender": str(message.author.id),
                        "sender_name": message.author.display_name,
                        "content": message.content,
                        "timestamp": message.created_at.isoformat(),
                        "metadata": {
                            "channel_id": str(message.channel.id),
                            "guild_id": str(message.guild.id) if message.guild else None,
                        },
                    }
                    
                    # Forward to gateway
                    if self._message_handler:
                        try:
                            await self._message_handler(msg_data)
                        except Exception as e:
                            logger.error(f"Error handling message: {e}")
            
            self._client = DiscordClient(intents=intents)
            self._client._message_handler = self._message_handler
            
            # Start the bot
            await self._client.start(self.token)
            self._running = True
            logger.info("Connected to Discord")
            
        except ImportError:
            raise RuntimeError(
                "discord.py not installed. "
                "Install with: pip install discord.py"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Discord: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect the Discord bot."""
        if self._client:
            await self._client.close()
            self._client = None
        self._running = False
        logger.info("Disconnected from Discord")
    
    async def send_message(
        self,
        target: Dict[str, Any],
        content: str,
    ) -> str:
        """Send a message to a Discord channel."""
        if not self._client:
            raise RuntimeError("Discord client not connected")
        
        channel_id = target.get("channel_id")
        if not channel_id:
            raise ValueError("No target channel_id provided")
        
        channel = self._client.get_channel(int(channel_id))
        if not channel:
            raise ValueError(f"Channel not found: {channel_id}")
        
        message = await channel.send(content)
        return str(message.id)
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        # This would need the message object - simplified for now
        pass
    
    async def handle_webhook(self, channel: str, data: dict) -> Optional[dict]:
        """Handle incoming webhook data."""
        if channel != "discord":
            return None
        
        # Transform to internal format
        return {
            "id": data.get("id"),
            "channel": "discord",
            "sender": data.get("sender"),
            "sender_name": data.get("sender_name"),
            "content": data.get("content"),
            "timestamp": data.get("timestamp"),
            "metadata": data.get("metadata", {}),
        }
    
    def set_message_handler(self, handler) -> None:
        """Set the message handler callback."""
        self._message_handler = handler
        if self._client:
            self._client._message_handler = handler


# Export the plugin class
Plugin = DiscordPlugin
