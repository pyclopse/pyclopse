"""Telegram channel adapter."""
import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from pyclaw.utils.time import now

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.telegram")


class TelegramAdapter(ChannelAdapter):
    """Telegram bot adapter using python-telegram-bot."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.token = config.get("bot_token")
        self.allowed_users = set(config.get("allowed_users", []))
        self._bot = None
        self._application = None
        self._update_queue: asyncio.Queue = asyncio.Queue()
    
    @property
    def channel_name(self) -> str:
        return "telegram"
    
    async def connect(self) -> None:
        """Initialize the Telegram bot."""
        try:
            from telegram import Bot
            from telegram.error import TelegramError
            
            self._bot = Bot(token=self.token)
            
            # Verify bot token by getting bot info
            me = await self._bot.get_me()
            logger.info(f"Connected to Telegram as @{me.username}")
            
        except ImportError:
            raise RuntimeError(
                "python-telegram-bot not installed. "
                "Install with: pip install python-telegram-bot"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Telegram: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect the Telegram bot."""
        if self._application:
            await self._application.stop()
        self._bot = None
        logger.info("Disconnected from Telegram")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a Telegram chat."""
        if not self._bot:
            raise RuntimeError("Telegram bot not connected")
        
        chat_id = target.user_id or target.group_id
        if not chat_id:
            raise ValueError("No target chat_id provided")
        
        kwargs = {
            "chat_id": chat_id,
            "text": content,
        }
        
        if reply_to or target.message_id:
            kwargs["reply_to_message_id"] = reply_to or target.message_id
        
        message = await self._bot.send_message(**kwargs)
        return str(message.message_id)
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media to a Telegram chat."""
        if not self._bot:
            raise RuntimeError("Telegram bot not connected")
        
        chat_id = target.user_id or target.group_id
        if not chat_id:
            raise ValueError("No target chat_id provided")
        
        if media.file_path:
            # Send local file
            if media.mime_type and media.mime_type.startswith("photo"):
                message = await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=open(media.file_path, "rb"),
                    caption=media.caption,
                )
            elif media.mime_type and media.mime_type.startswith("video"):
                message = await self._bot.send_video(
                    chat_id=chat_id,
                    video=open(media.file_path, "rb"),
                    caption=media.caption,
                )
            else:
                message = await self._bot.send_document(
                    chat_id=chat_id,
                    document=open(media.file_path, "rb"),
                    caption=media.caption,
                )
        elif media.url:
            # Send by URL
            if media.mime_type and media.mime_type.startswith("photo"):
                message = await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=media.url,
                    caption=media.caption,
                )
            elif media.mime_type and media.mime_type.startswith("video"):
                message = await self._bot.send_video(
                    chat_id=chat_id,
                    video=media.url,
                    caption=media.caption,
                )
            else:
                message = await self._bot.send_document(
                    chat_id=chat_id,
                    document=media.url,
                    caption=media.caption,
                )
        else:
            raise ValueError("No file_path or URL provided for media")
        
        return str(message.message_id)
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        if not self._bot:
            raise RuntimeError("Telegram bot not connected")
        
        # Telegram uses emoji codes
        await self._bot.set_message_reaction(
            chat_id=int(self.config.get("chat_id", 0)),
            message_id=int(message_id),
            reaction=[{"type": "emoji", "emoji": emoji}],
        )
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming Telegram webhook."""
        try:
            from telegram import Update
            
            update = Update.de_json(payload, self._bot)
            
            if not update or not update.message:
                return None
            
            msg = update.message
            
            # Check allowed users
            if self.allowed_users and msg.from_user.id not in self.allowed_users:
                logger.debug(
                    f"Ignored message from unauthorized user {msg.from_user.id}"
                )
                return None
            
            return Message(
                id=str(msg.message_id),
                channel="telegram",
                sender=str(msg.from_user.id),
                sender_name=msg.from_user.name or msg.from_user.username or "Unknown",
                content=msg.text or "",
                timestamp=msg.date or now(),
                metadata={
                    "chat_id": str(msg.chat.id),
                    "chat_type": msg.chat.type,
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling Telegram webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Listen for updates (polling fallback)."""
        # This is a fallback for when webhooks aren't used
        # In production, webhooks are preferred
        offset = None
        
        while self._running:
            try:
                updates = await self._bot.get_updates(
                    offset=offset,
                    timeout=30,
                )
                
                for update in updates:
                    if update.message:
                        message = await self._parse_update(update)
                        if message:
                            await self._dispatch(message)
                            offset = update.update_id + 1
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error polling Telegram: {e}")
                await asyncio.sleep(5)
    
    async def _parse_update(self, update: Any) -> Optional[Message]:
        """Parse a Telegram update into a Message."""
        from telegram import Update
        
        if not isinstance(update, Update) or not update.message:
            return None
        
        msg = update.message
        
        # Check allowed users
        if self.allowed_users and msg.from_user.id not in self.allowed_users:
            return None
        
        return Message(
            id=str(msg.message_id),
            channel="telegram",
            sender=str(msg.from_user.id),
            sender_name=msg.from_user.name or msg.from_user.username or "Unknown",
            content=msg.text or "",
            timestamp=msg.date or now(),
            metadata={
                "chat_id": str(msg.chat.id),
                "chat_type": msg.chat.type,
            },
        )
