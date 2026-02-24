"""LINE channel adapter."""
import asyncio
import hashlib
import hmac
import base64
import logging
from typing import Any, Dict, Optional
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.line")


def validate_line_signature(body: str, signature: str, channel_secret: str) -> bool:
    """Validate LINE webhook signature."""
    hash = hmac.new(
        channel_secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    )
    return base64.b64encode(hash.digest()).decode('utf-8') == signature


class LineAdapter(ChannelAdapter):
    """
    LINE messaging platform adapter using LINE Messaging API.
    
    Requires:
    - LINE Channel Access Token
    - LINE Channel Secret
    
    Docs: https://developers.line.biz/en/docs/messaging-api/
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.channel_access_token = config.get("channel_access_token")
        self.channel_secret = config.get("channel_secret")
        self.user_id = config.get("user_id")  # Default user for sends
        self._session = None
    
    @property
    def channel_name(self) -> str:
        return "line"
    
    async def connect(self) -> None:
        """Initialize the LINE API client."""
        try:
            import httpx
            
            self._session = httpx.AsyncClient(
                base_url="https://api.line.me/v2",
                headers={
                    "Authorization": f"Bearer {self.channel_access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            
            # Verify token by getting bot info
            response = await self._session.get("/bot/info")
            if response.status_code != 200:
                raise RuntimeError(f"LINE API error: {response.text}")
            
            bot_info = response.json()
            logger.info(f"Connected to LINE as @{bot_info.get('userId', 'unknown')}")
            
        except ImportError:
            raise RuntimeError(
                "httpx not installed. "
                "Install with: pip install httpx"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to LINE: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect from LINE."""
        if self._session:
            await self._session.aclose()
        self._session = None
        logger.info("Disconnected from LINE")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a LINE user/group."""
        if not self._session:
            raise RuntimeError("LINE client not connected")
        
        to = target.user_id or target.group_id
        if not to:
            raise ValueError("No target user_id or group_id provided")
        
        payload = {
            "to": to,
            "messages": [
                {"type": "text", "text": content}
            ]
        }
        
        if reply_to:
            # Use reply API instead of push
            payload = {
                "replyToken": reply_to,
                "messages": [
                    {"type": "text", "text": content}
                ]
            }
            response = await self._session.post("/bot/message/reply", json=payload)
        else:
            response = await self._session.post("/bot/message/push", json=payload)
        
        if response.status_code != 200:
            raise RuntimeError(f"LINE API error: {response.text}")
        
        result = response.json()
        return result.get("sentMessages", [{}])[0].get("messageId", "unknown")
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media (image, video, etc.) to a LINE user/group."""
        if not self._session:
            raise RuntimeError("LINE client not connected")
        
        to = target.user_id or target.group_id
        if not to:
            raise ValueError("No target user_id or group_id provided")
        
        message_type = "image"
        if media.mime_type:
            if media.mime_type.startswith("video"):
                message_type = "video"
            elif media.mime_type.startswith("audio"):
                message_type = "audio"
        
        message = {
            "type": message_type,
        }
        
        # Set URL or use uploaded content
        if media.url:
            message["originalContentUrl"] = media.url
            message["previewImageUrl"] = media.url
        elif media.file_path:
            # For file_path, we'd need to upload first
            # For now, use placeholder
            raise NotImplementedError("File upload not yet implemented")
        
        if media.caption:
            message["type"] = "text"  # Send as text with media reference
        
        payload = {
            "to": to,
            "messages": [message]
        }
        
        response = await self._session.post("/bot/message/push", json=payload)
        
        if response.status_code != 200:
            raise RuntimeError(f"LINE API error: {response.text}")
        
        result = response.json()
        return result.get("sentMessages", [{}])[0].get("messageId", "unknown")
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        # LINE doesn't have a direct reaction API like Telegram
        # Could use emoji in a follow-up message
        logger.debug(f"LINE doesn't support reactions, ignoring: {emoji}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming LINE webhook."""
        try:
            events = payload.get("events", [])
            if not events:
                return None
            
            # Process first event
            event = events[0]
            event_type = event.get("type")
            
            if event_type == "message":
                msg_event = event.get("message", {})
                return Message(
                    id=msg_event.get("id", ""),
                    channel="line",
                    sender=event.get("source", {}).get("userId", ""),
                    sender_name=event.get("source", {}).get("userId", "Unknown"),
                    content=msg_event.get("text", ""),
                    timestamp=datetime.now(),
                    metadata={
                        "replyToken": event.get("replyToken"),
                        "source_type": event.get("source", {}).get("type"),
                        "group_id": event.get("source", {}).get("groupId"),
                        "room_id": event.get("source", {}).get("roomId"),
                    },
                    reply_to=event.get("replyToken"),
                )
            
            elif event_type == "follow":
                return Message(
                    id=f"follow-{event.get('timestamp', '')}",
                    channel="line",
                    sender=event.get("source", {}).get("userId", ""),
                    sender_name="LINE User",
                    content="[follow]",
                    timestamp=datetime.now(),
                )
            
            elif event_type == "unfollow":
                return Message(
                    id=f"unfollow-{event.get('timestamp', '')}",
                    channel="line",
                    sender=event.get("source", {}).get("userId", ""),
                    sender_name="LINE User",
                    content="[unfollow]",
                    timestamp=datetime.now(),
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error handling LINE webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Listen for messages (polling fallback)."""
        # LINE uses webhooks primarily, but we can poll for messages
        # This is not recommended for production
        while self._running:
            await asyncio.sleep(30)
