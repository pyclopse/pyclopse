"""Telegram channel plugin."""

import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime

from pyclaw.plugins import ChannelPlugin, PluginMetadata, PluginType


logger = logging.getLogger("pyclaw.plugins.telegram")


class TelegramPlugin(ChannelPlugin):
    """Telegram bot plugin using python-telegram-bot."""
    
    VERSION = "1.0.0"
    
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="telegram",
            version=self.VERSION,
            description="Telegram messaging channel plugin",
            author="pyclaw",
            plugin_type=PluginType.CHANNEL,
            tags=["messaging", "telegram"],
        )
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.token = config.get("bot_token")
        self.allowed_users = set(config.get("allowed_users", []))
        self._bot = None
        self._application = None
        self._update_queue: asyncio.Queue = asyncio.Queue()
        self._message_handler = None
        self._running = False
        self._listener_task: Optional[asyncio.Task] = None
    
    @property
    def channel_name(self) -> str:
        return "telegram"
    
    @property
    def is_connected(self) -> bool:
        return self._running and self._bot is not None
    
    async def on_load(self, gateway) -> None:
        """Initialize the Telegram bot."""
        await super().on_load(gateway)
        await self.connect()
    
    async def on_unload(self) -> None:
        """Cleanup on unload."""
        await self.disconnect()
        await super().on_unload()
    
    async def connect(self) -> None:
        """Initialize the Telegram bot."""
        try:
            from telegram import Bot
            from telegram.error import TelegramError
            
            self._bot = Bot(token=self.token)
            
            # Verify bot token by getting bot info
            me = await self._bot.get_me()
            logger.info(f"Connected to Telegram as @{me.username}")
            
            # Start listening
            await self.start_listening()
            
        except ImportError:
            raise RuntimeError(
                "python-telegram-bot not installed. "
                "Install with: pip install python-telegram-bot"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Telegram: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect the Telegram bot."""
        await self.stop_listening()
        
        if self._application:
            await self._application.stop()
        self._bot = None
        logger.info("Disconnected from Telegram")
    
    async def start_listening(self) -> None:
        """Start polling for updates."""
        if self._running:
            return
        
        self._running = True
        self._listener_task = asyncio.create_task(
            self._poll_updates(),
            name="telegram-poll"
        )
        logger.info("Started Telegram update polling")
    
    async def stop_listening(self) -> None:
        """Stop polling for updates."""
        self._running = False
        
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
    
    async def _poll_updates(self) -> None:
        """Poll Telegram for updates."""
        from telegram import Update
        from telegram.error import TelegramError
        
        offset = None
        
        while self._running:
            try:
                updates = await self._bot.get_updates(
                    timeout=30,
                    offset=offset,
                )
                
                for update in updates:
                    if update.update_id:
                        offset = update.update_id + 1
                    
                    if update.message or update.callback_query:
                        await self._handle_update(update)
                
            except TelegramError as e:
                logger.error(f"Telegram polling error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error in polling: {e}")
                await asyncio.sleep(5)
    
    async def _handle_update(self, update) -> None:
        """Handle incoming Telegram update."""
        from telegram import Update, Message
        
        if isinstance(update, Update):
            message = update.message or update.callback_query.message
            
            if message:
                # Build internal message format
                msg_data = {
                    "id": str(message.message_id),
                    "channel": "telegram",
                    "sender": str(message.from_user.id),
                    "sender_name": message.from_user.full_name,
                    "content": message.text or "",
                    "timestamp": message.date.isoformat() if message.date else datetime.now().isoformat(),
                    "metadata": {
                        "chat_id": str(message.chat.id),
                        "chat_type": message.chat.type,
                    },
                }
                
                # Handle via webhook for consistency
                if self._gateway:
                    await self.handle_webhook("telegram", msg_data)
    
    async def send_message(
        self,
        target: Dict[str, Any],
        content: str,
    ) -> str:
        """Send a message to a Telegram chat."""
        if not self._bot:
            raise RuntimeError("Telegram bot not connected")
        
        chat_id = target.get("user_id") or target.get("group_id")
        if not chat_id:
            raise ValueError("No target chat_id provided")
        
        kwargs = {
            "chat_id": chat_id,
            "text": content,
        }
        
        reply_to = target.get("message_id")
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        
        message = await self._bot.send_message(**kwargs)
        return str(message.message_id)
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        # Not implemented - requires bot to be a member of the chat
        pass
    
    async def handle_webhook(self, channel: str, data: dict) -> Optional[dict]:
        """Handle incoming webhook data."""
        if channel != "telegram":
            return None
        
        # Transform to internal format
        return {
            "id": data.get("id"),
            "channel": "telegram",
            "sender": data.get("sender"),
            "sender_name": data.get("sender_name"),
            "content": data.get("content"),
            "timestamp": data.get("timestamp"),
            "metadata": data.get("metadata", {}),
        }
    
    def set_message_handler(self, handler) -> None:
        """Set the message handler callback."""
        self._message_handler = handler


# Export the plugin class
Plugin = TelegramPlugin
