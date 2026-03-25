"""Discord channel adapter."""
import asyncio
import logging
import json
from typing import Any, Dict, Optional
from datetime import datetime
from pyclawops.utils.time import now

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclawops.channels.discord")


class DiscordAdapter(ChannelAdapter):
    """Discord bot adapter using discord.py.

    Listens for guild messages via the discord.py event system and also
    supports webhook-based message parsing. Requires the ``discord.py``
    package.

    Attributes:
        token (Optional[str]): Discord bot token.
        guild_ids (set): Set of guild (server) IDs to listen to. If non-empty,
            messages from guilds not in this set are silently ignored.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the Discord adapter with bot configuration.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``bot_token`` (str): Discord bot token.
                ``guilds`` (list): Optional list of guild IDs to restrict
                    message handling to.
        """
        super().__init__(config)
        self.token = config.get("bot_token")
        self.guild_ids = set(config.get("guilds", []))
        self._bot = None
        self._client = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"discord"``.
        """
        return "discord"

    async def connect(self) -> None:
        """Initialize the Discord bot and start listening for messages.
        Creates a ``commands.Bot`` with message content and guild intents,
        registers ``on_ready`` and ``on_message`` event handlers, and calls
        ``client.start()``.

        Raises:
            RuntimeError: If ``discord.py`` is not installed or if the bot
                fails to connect.
        """
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
                """Log a message when the bot has connected and is ready."""
                logger.info(f"Logged in as {self._client.user}")

            @self._client.event
            async def on_message(message: discord.Message):
                """Handle an incoming Discord message event.

                Ignores bot messages and messages from guilds not in the
                ``guild_ids`` filter, then dispatches the message to the
                registered handler.

                Args:
                    message (discord.Message): The Discord message object
                        received from the gateway.
                """
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
        """Disconnect the Discord bot and release resources.
        Closes the discord.py client and clears the reference.
        """
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
        """Send a text message to a Discord channel.

        Args:
            target (MessageTarget): Destination. Uses ``target.channel_id``
                or ``target.group_id`` as the Discord channel ID (integer).
            content (str): Text content to send.
            reply_to (Optional[str]): Message ID to create a reply reference
                for. Defaults to None.

        Returns:
            str: Discord snowflake ID of the sent message as a string.

        Raises:
            RuntimeError: If the Discord client is not connected.
            ValueError: If the channel cannot be found by the given ID.
        """
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
        """Send a media attachment to a Discord channel.

        Sends a local file as a ``discord.File`` attachment, or embeds a URL
        image using a ``discord.Embed``.

        Args:
            target (MessageTarget): Destination channel. Uses
                ``target.channel_id`` or ``target.group_id``.
            media (MediaAttachment): Media to send. Either ``file_path`` or
                ``url`` must be set.

        Returns:
            str: Discord snowflake ID of the sent message as a string.

        Raises:
            RuntimeError: If the Discord client is not connected.
            ValueError: If the channel cannot be found or if neither
                ``media.file_path`` nor ``media.url`` is provided.
        """
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
        """Add an emoji reaction to a Discord message.

        Supports both Unicode emoji characters and Discord custom emote strings
        (format: ``<:name:id>``). Errors during reaction are logged but do not
        propagate.

        Args:
            message_id (str): Discord snowflake ID of the message to react to.
            emoji (str): Unicode emoji character or Discord custom emote string.
        """
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
        """Parse and handle an incoming Discord Gateway webhook payload.

        Processes ``MESSAGE_CREATE`` dispatch events. Ignores bot messages and
        messages from guilds not in the ``guild_ids`` filter.

        Args:
            payload (Dict[str, Any]): Raw Discord Gateway JSON payload with
                ``t`` (event type) and ``d`` (event data) fields.

        Returns:
            Optional[Message]: Parsed message, or ``None`` if the payload
                should be ignored.
        """
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
                timestamp=now(),  # Discord doesn't send timestamp in webhook
                metadata={
                    "guild_id": guild_id,
                    "channel_id": msg_data.get("channel_id"),
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling Discord webhook: {e}")
            return None


class DiscordWebhookAdapter(ChannelAdapter):
    """Simplified Discord adapter that sends messages via Discord webhooks.

    This adapter can only send outbound messages. It does not support
    receiving messages, reactions, or any feature that requires the full
    Discord bot API. Useful for simple notification use-cases.

    Attributes:
        webhook_url (Optional[str]): Discord webhook URL to POST messages to.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the Discord webhook adapter.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``webhook_url`` (str): Discord webhook URL.
        """
        super().__init__(config)
        self.webhook_url = config.get("webhook_url")
        self._session = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"discord_webhook"``.
        """
        return "discord_webhook"

    async def connect(self) -> None:
        """Initialize the HTTP session for sending webhook requests.
        Creates an ``httpx.AsyncClient`` for making requests to the webhook URL.
        """
        import httpx
        self._session = httpx.AsyncClient()
        logger.info("Discord webhook adapter initialized")

    async def disconnect(self) -> None:
        """Close the HTTP session and release resources.
        Calls ``aclose()`` on the httpx client if it exists.
        """
        if self._session:
            await self._session.aclose()
        self._session = None

    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message via Discord webhook URL.

        POSTs a JSON payload to the configured webhook URL.

        Args:
            target (MessageTarget): Ignored — the webhook URL determines the
                destination channel.
            content (str): Text content to send.
            reply_to (Optional[str]): Message ID to add a ``message_reference``
                for. Defaults to None.

        Returns:
            str: Discord snowflake ID of the created message, or an empty
                string if the response does not include one.

        Raises:
            RuntimeError: If the HTTP session or webhook URL is not configured.
            httpx.HTTPStatusError: If the Discord API returns a non-2xx status.
        """
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
        """Not supported in webhook-only mode.

        Args:
            message_id (str): Unused.
            emoji (str): Unused.

        Raises:
            NotImplementedError: Always — reactions require the full bot API.
                Use :class:`DiscordAdapter` instead.
        """
        raise NotImplementedError(
            "Reactions not supported in webhook mode. Use DiscordAdapter."
        )
