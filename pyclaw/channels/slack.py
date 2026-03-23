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
    """Slack bot adapter using slack-sdk.

    Supports webhook-based message reception. Requires the ``slack-sdk``
    package.

    Attributes:
        bot_token (Optional[str]): Slack bot OAuth token (``xoxb-...``).
        signing_secret (Optional[str]): Slack signing secret used to verify
            incoming webhook requests.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the Slack adapter with bot configuration.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``bot_token`` (str): Slack bot OAuth token.
                ``signing_secret`` (str): Slack signing secret for request
                    verification.
        """
        super().__init__(config)
        self.bot_token = config.get("bot_token")
        self.signing_secret = config.get("signing_secret")
        self._client = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"slack"``.
        """
        return "slack"

    async def connect(self) -> None:
        """Initialize and verify the Slack client connection.
        Creates a Slack ``WebClient`` and verifies the token by calling
        ``auth_test()``.

        Raises:
            RuntimeError: If ``slack-sdk`` is not installed or if
                authentication fails.
        """
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
        """Disconnect the Slack client and release resources.
        Clears the client reference to allow garbage collection.
        """
        self._client = None
        logger.info("Disconnected from Slack")

    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message to a Slack channel.

        Args:
            target (MessageTarget): Destination. Uses ``target.channel_id``
                or ``target.group_id`` as the Slack channel ID.
            content (str): Text content to send.
            reply_to (Optional[str]): Thread timestamp to reply in-thread.
                Defaults to None.

        Returns:
            str: Slack message timestamp (``ts``) of the sent message.

        Raises:
            RuntimeError: If the Slack client is not connected.
            ValueError: If neither ``target.channel_id`` nor
                ``target.group_id`` is set.
        """
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
        """Send a media attachment to a Slack channel.

        Uploads a local file using ``files_upload``, or posts a message
        with an image attachment using ``chat_postMessage`` when only a URL
        is provided.

        Args:
            target (MessageTarget): Destination channel. Uses
                ``target.channel_id`` or ``target.group_id``.
            media (MediaAttachment): Media to send. Either ``file_path`` or
                ``url`` must be set.

        Returns:
            str: Slack message timestamp (``ts``) of the sent message, or
                an empty string if the response does not include one.

        Raises:
            RuntimeError: If the Slack client is not connected.
            ValueError: If no channel ID is set or if neither ``media.file_path``
                nor ``media.url`` is provided.
        """
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
        """Add an emoji reaction to a Slack message.

        Normalizes the emoji to a bare name (strips surrounding colons) and
        calls ``reactions_add``.

        Args:
            message_id (str): Slack message timestamp to react to.
            emoji (str): Emoji name with or without surrounding colons, e.g.
                ``"thumbsup"`` or ``":thumbsup:"``.

        Raises:
            RuntimeError: If the Slack client is not connected.
            ValueError: If ``channel_id`` is not set in the adapter config.
        """
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
        """Parse and handle an incoming Slack Events API webhook payload.

        Handles ``event_callback`` payloads containing ``message`` events.
        Silently ignores URL verification challenges, bot messages, non-message
        events, and messages without text.

        Args:
            payload (Dict[str, Any]): Raw JSON webhook payload from Slack.

        Returns:
            Optional[Message]: Parsed message, or ``None`` if the payload
                should be ignored.
        """
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
        """Verify a Slack request signature using HMAC-SHA256.

        Computes the expected signature from the signing secret and compares
        it to the provided signature using a constant-time comparison to
        prevent timing attacks.

        Args:
            payload (str): Raw request body as a string.
            timestamp (str): Value of the ``X-Slack-Request-Timestamp`` header.
            signature (str): Value of the ``X-Slack-Signature`` header
                (format: ``v0=<hex>``).

        Returns:
            bool: ``True`` if the signature is valid, ``False`` otherwise or
                if no signing secret is configured.
        """
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
        """No-op polling fallback for Slack.

        Slack uses webhooks for inbound messages. This method is a no-op
        placeholder. In production, configure the Slack Events API to deliver
        messages via ``handle_webhook``.
        """
        # This is a fallback for when webhooks aren't used
        # In production, webhooks are preferred
        pass
