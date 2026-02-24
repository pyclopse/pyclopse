"""Discord channel adapter."""
import asyncio
import logging
import json
from typing import Any, Dict, Optional
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.discord")


class DiscordAdapter(ChannelAdapter):
    """Discord bot adapter using discord.py."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.token = config.get("bot_token")
        self.guild_ids = set(config.get("guilds", []))
        self._bot = None
        self._client = None
    
    @property
    def channel_name(self) -> str:
        return "discord"
    
    async def connect(self) -> None:
        """Initialize the Discord bot."""
        try:
            import discord
            from discord.ext import commands
            
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True
            
            self._client = commands.Bot(
                command_prefix="!",
                intents=intents,
            )
            
            @self._client.event
            async def on_ready():
                logger.info(f"Logged in as {self._client.user}")
            
            @self._client.event
            async def on_message(message: discord.Message):
                # Ignore bot messages
                if message.author.bot:
                    return
                
                # Check guild filter
                if self.guild_ids and message.guild.id not in self.guild_ids:
                    return
                
                msg = Message(
                    id=str(message.id),
                    channel="discord",
                    sender=str(message.author.id),
                    sender_name=message.author.display_name,
                    content=message.content,
                    timestamp=message.created_at,
                    metadata={
                        "guild_id": str(message.guild.id) if message.guild else None,
                        "channel_id": str(message.channel.id),
                        "channel_name": message.channel.name if hasattr(message.channel, 'name') else None,
                    },
                )
                await self._dispatch(msg)
            
            # Start the bot
            await self._client.start(self.token)
            
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
        logger.info("Disconnected from Discord")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a Discord channel."""
        if not self._client:
            raise RuntimeError("Discord client not connected")
        
        import discord
        
        channel_id = int(target.channel_id or target.group_id or 0)
        channel = self._client.get_channel(channel_id)
        
        if not channel:
            raise ValueError(f"Could not find channel with ID {channel_id}")
        
        # Handle reply
        reference = None
        if reply_to:
            reference = discord.MessageReference(
                message_id=int(reply_to),
                channel_id=channel_id,
            )
        
        message = await channel.send(content, reference=reference)
        return str(message.id)
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media to a Discord channel."""
        if not self._client:
            raise RuntimeError("Discord client not connected")
        
        import discord
        
        channel_id = int(target.channel_id or target.group_id or 0)
        channel = self._client.get_channel(channel_id)
        
        if not channel:
            raise ValueError(f"Could not find channel with ID {channel_id}")
        
        if media.file_path:
            file = discord.File(media.file_path)
            message = await channel.send(
                media.caption or "",
                file=file,
            )
        elif media.url:
            # Discord embeds URLs automatically
            message = await channel.send(
                content=media.caption or "",
                embed=discord.Embed().set_image(url=media.url),
            )
        else:
            raise ValueError("No file_path or URL provided for media")
        
        return str(message.id)
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        if not self._client:
            raise RuntimeError("Discord client not connected")
        
        import discord
        
        # Parse emoji (could be unicode or custom)
        channel_id = int(self.config.get("channel_id", 0))
        channel = self._client.get_channel(channel_id)
        
        if not channel:
            return
        
        try:
            # Try to get the message
            message = await channel.fetch_message(int(message_id))
            
            # Convert emoji string to emoji object
            # Handle both unicode and custom emotes
            if emoji.startswith("<:") and emoji.endswith(">"):
                # Custom emote
                emoji_obj = discord.PartialEmoji.from_str(emoji)
            else:
                # Unicode emoji
                emoji_obj = emoji
            
            await message.add_reaction(emoji_obj)
            
        except Exception as e:
            logger.error(f"Error adding reaction: {e}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming Discord webhook."""
        try:
            import discord
            
            # Discord sends JSON payload
            data = payload
            
            # Check for message type
            if data.get("t") != "MESSAGE_CREATE":
                return None
            
            msg_data = data.get("d", {})
            
            # Skip bot messages
            if msg_data.get("author", {}).get("bot"):
                return None
            
            # Check guild filter
            guild_id = msg_data.get("guild_id")
            if self.guild_ids and guild_id and int(guild_id) not in self.guild_ids:
                return None
            
            return Message(
                id=msg_data.get("id", ""),
                channel="discord",
                sender=msg_data.get("author", {}).get("id", ""),
                sender_name=msg_data.get("author", {}).get("username", "Unknown"),
                content=msg_data.get("content", ""),
                timestamp=datetime.now(),  # Discord doesn't send timestamp in webhook
                metadata={
                    "guild_id": guild_id,
                    "channel_id": msg_data.get("channel_id"),
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling Discord webhook: {e}")
            return None


class DiscordWebhookAdapter(ChannelAdapter):
    """
    Simplified Discord adapter using webhooks only.
    Useful for when you don't need the full bot.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.webhook_url = config.get("webhook_url")
        self._session = None
    
    @property
    def channel_name(self) -> str:
        return "discord_webhook"
    
    async def connect(self) -> None:
        """Initialize HTTP session for webhooks."""
        import httpx
        self._session = httpx.AsyncClient()
        logger.info("Discord webhook adapter initialized")
    
    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.aclose()
        self._session = None
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message via Discord webhook."""
        if not self._session or not self.webhook_url:
            raise RuntimeError("Discord webhook not configured")
        
        payload = {"content": content}
        
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to}
        
        response = await self._session.post(
            self.webhook_url,
            json=payload,
        )
        response.raise_for_status()
        
        # Webhook responses include the created message
        data = response.json()
        return str(data.get("id", ""))
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Reactions require the bot API, not webhooks."""
        raise NotImplementedError(
            "Reactions not supported in webhook mode. Use DiscordAdapter."
        )
