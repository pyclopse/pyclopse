"""Slack channel adapter."""
import asyncio
import logging
import hashlib
import hmac
import time
from typing import Any, Dict, Optional
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.slack")


class SlackAdapter(ChannelAdapter):
    """Slack bot adapter using slack-sdk."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.bot_token = config.get("bot_token")
        self.signing_secret = config.get("signing_secret")
        self._client = None
    
    @property
    def channel_name(self) -> str:
        return "slack"
    
    async def connect(self) -> None:
        """Initialize the Slack client."""
        try:
            from slack_sdk import WebClient
            
            self._client = WebClient(token=self.bot_token)
            
            # Verify token by getting auth info
            auth = await self._client.auth_test()
            logger.info(f"Connected to Slack as @{auth['user']}")
            
        except ImportError:
            raise RuntimeError(
                "slack-sdk not installed. "
                "Install with: pip install slack-sdk"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Slack: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect the Slack client."""
        self._client = None
        logger.info("Disconnected from Slack")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a Slack channel."""
        if not self._client:
            raise RuntimeError("Slack client not connected")
        
        channel_id = target.channel_id or target.group_id
        if not channel_id:
            raise ValueError("No target channel_id provided")
        
        kwargs = {
            "channel": channel_id,
            "text": content,
        }
        
        if reply_to:
            kwargs["thread_ts"] = reply_to
        
        response = await self._client.chat_postMessage(**kwargs)
        return response["ts"]
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media to a Slack channel."""
        if not self._client:
            raise RuntimeError("Slack client not connected")
        
        channel_id = target.channel_id or target.group_id
        if not channel_id:
            raise ValueError("No target channel_id provided")
        
        if media.file_path:
            response = await self._client.files_upload(
                channel=channel_id,
                file=media.file_path,
                title=media.caption,
            )
        elif media.url:
            # Upload from URL requires downloading first
            response = await self._client.chat_postMessage(
                channel=channel_id,
                text=media.caption or "",
                attachments=[{"image_url": media.url}],
            )
        else:
            raise ValueError("No file_path or URL provided for media")
        
        return response.get("ts", "")
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        if not self._client:
            raise RuntimeError("Slack client not connected")
        
        # Extract channel from config or metadata
        channel_id = self.config.get("channel_id")
        if not channel_id:
            raise ValueError("channel_id not configured")
        
        # Convert emoji to Slack format
        # Slack uses colon-wrapped emoji names
        if not emoji.startswith(":"):
            emoji = f":{emoji}:"
        
        await self._client.reactions_add(
            channel=channel_id,
            timestamp=message_id,
            name=emoji.strip(":"),
        )
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming Slack webhook."""
        try:
            # Handle URL verification challenge
            if payload.get("type") == "url_verification":
                # Return the challenge for verification
                return None
            
            # Handle event callback
            if payload.get("type") != "event_callback":
                return None
            
            event = payload.get("event", {})
            
            # Skip bot messages
            if event.get("subtype") == "bot_message":
                return None
            
            # Handle message events
            if event.get("type") != "message":
                return None
            
            # Skip messages without text
            if not event.get("text"):
                return None
            
            return Message(
                id=event.get("ts", ""),
                channel="slack",
                sender=event.get("user", ""),
                sender_name=event.get("username", "Unknown"),
                content=event.get("text", ""),
                timestamp=datetime.fromtimestamp(float(event.get("ts", 0))),
                metadata={
                    "channel_id": event.get("channel"),
                    "thread_ts": event.get("thread_ts"),
                },
                reply_to=event.get("thread_ts") if event.get("thread_ts") != event.get("ts") else None,
            )
            
        except Exception as e:
            logger.error(f"Error handling Slack webhook: {e}")
            return None
    
    def verify_signature(self, payload: str, timestamp: str, signature: str) -> bool:
        """Verify Slack request signature."""
        if not self.signing_secret:
            return False
        
        # Create base string
        base_string = f"v0:{timestamp}:{payload}"
        
        # Calculate signature
        hash = hmac.new(
            self.signing_secret.encode(),
            base_string.encode(),
            hashlib.sha256,
        )
        expected_signature = f"v0={hash.hexdigest()}"
        
        # Compare signatures
        return hmac.compare_digest(expected_signature, signature)
    
    async def _listen(self) -> None:
        """Listen for updates (polling fallback)."""
        # This is a fallback for when webhooks aren't used
        # In production, webhooks are preferred
        pass
