"""LINE channel adapter."""
import asyncio
import hashlib
import hmac
import base64
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from pyclaw.utils.time import now

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.line")


def validate_line_signature(body: str, signature: str, channel_secret: str) -> bool:
    """Validate a LINE webhook signature using HMAC-SHA256.

    Computes the expected signature from the channel secret and the raw
    request body, then compares it to the provided signature.

    Args:
        body (str): Raw request body as a UTF-8 string.
        signature (str): Base64-encoded signature from the
            ``X-Line-Signature`` header.
        channel_secret (str): LINE channel secret used to compute the HMAC.

    Returns:
        bool: ``True`` if the computed signature matches the provided one,
            ``False`` otherwise.
    """
    hash = hmac.new(
        channel_secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    )
    return base64.b64encode(hash.digest()).decode('utf-8') == signature


class LineAdapter(ChannelAdapter):
    """LINE messaging platform adapter using the LINE Messaging API.

    Requires a LINE Channel Access Token and Channel Secret obtained from
    the LINE Developers Console.

    See: https://developers.line.biz/en/docs/messaging-api/

    Attributes:
        channel_access_token (Optional[str]): LINE Channel Access Token for
            API authentication.
        channel_secret (Optional[str]): LINE Channel Secret for webhook
            signature verification.
        user_id (Optional[str]): Default user ID to send messages to when
            no explicit target is given.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the LINE adapter with API credentials.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``channel_access_token`` (str): LINE Channel Access Token.
                ``channel_secret`` (str): LINE Channel Secret.
                ``user_id`` (str): Optional default user ID for sends.
        """
        super().__init__(config)
        self.channel_access_token = config.get("channel_access_token")
        self.channel_secret = config.get("channel_secret")
        self.user_id = config.get("user_id")  # Default user for sends
        self._session = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"line"``.
        """
        return "line"

    async def connect(self) -> None:
        """Initialize the LINE API client and verify the access token.
        Creates an httpx session with the LINE API base URL and authorization
        header, then calls ``/bot/info`` to verify the token.

        Raises:
            RuntimeError: If ``httpx`` is not installed or if the token
                verification fails.
        """
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
        """Disconnect from LINE and release resources.

        Closes the httpx session if one is open.
        """
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
        """Send a text message to a LINE user or group.

        Uses the LINE Reply API when ``reply_to`` is provided (treating it as
        a reply token), or the Push API for direct sends.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` or
                ``target.group_id`` as the LINE recipient ID.
            content (str): Text content to send.
            reply_to (Optional[str]): LINE reply token from an incoming event.
                When set, the Reply API is used instead of Push. Defaults to
                None.

        Returns:
            str: LINE message ID of the first sent message, or ``"unknown"``
                if absent.

        Raises:
            RuntimeError: If the LINE client is not connected or the API
                returns an error.
            ValueError: If neither ``target.user_id`` nor ``target.group_id``
                is set.
        """
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
        """Send a media message (image, video, audio) to a LINE user or group.

        Selects the LINE message type based on ``media.mime_type``. Requires
        a public URL via ``media.url``; local file upload is not yet
        implemented.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` or
                ``target.group_id``.
            media (MediaAttachment): Media to send. ``url`` must be set.

        Returns:
            str: LINE message ID of the sent message, or ``"unknown"`` if
                absent.

        Raises:
            RuntimeError: If the LINE client is not connected or the API
                returns an error.
            NotImplementedError: If ``media.file_path`` is set (file upload
                is not implemented).
            ValueError: If no target is set.
        """
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
        """Add an emoji reaction to a LINE message (not supported).

        LINE does not provide a direct reaction API. This method is a no-op
        and logs a debug message.

        Args:
            message_id (str): Unused.
            emoji (str): Unused.
        """
        # LINE doesn't have a direct reaction API like Telegram
        # Could use emoji in a follow-up message
        logger.debug(f"LINE doesn't support reactions, ignoring: {emoji}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Parse and handle an incoming LINE webhook payload.

        Processes the first event in the ``events`` array. Handles
        ``message``, ``follow``, and ``unfollow`` event types.

        Args:
            payload (Dict[str, Any]): Raw JSON webhook payload from LINE.

        Returns:
            Optional[Message]: Parsed message for supported event types,
                ``None`` if the events list is empty, the event type is
                unsupported, or a parse error occurs.
        """
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
                    timestamp=now(),
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
                    timestamp=now(),
                )
            
            elif event_type == "unfollow":
                return Message(
                    id=f"unfollow-{event.get('timestamp', '')}",
                    channel="line",
                    sender=event.get("source", {}).get("userId", ""),
                    sender_name="LINE User",
                    content="[unfollow]",
                    timestamp=now(),
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error handling LINE webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """No-op polling fallback for LINE.

        LINE uses webhooks for inbound message delivery. This method sleeps
        in a loop as a placeholder. Polling is not recommended for production;
        configure the LINE webhook URL to use ``handle_webhook`` instead.
        """
        # LINE uses webhooks primarily, but we can poll for messages
        # This is not recommended for production
        while self._running:
            await asyncio.sleep(30)
