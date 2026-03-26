"""WhatsApp channel adapter using WhatsApp Business Cloud API."""
import asyncio
import logging
import hashlib
import hmac
import base64
from typing import Any, Dict, Optional
from datetime import datetime
from pyclopse.utils.time import now

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclopse.channels.whatsapp")


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp Business Cloud API adapter.

    Uses the Meta Graph API (``graph.facebook.com``) to send and receive
    WhatsApp messages. Requires a WhatsApp Business account and a Meta
    Developer app with the WhatsApp product enabled.

    Attributes:
        phone_number_id (Optional[str]): WhatsApp Business phone number ID
            from the Meta Developer dashboard.
        access_token (Optional[str]): Meta access token for API calls.
        verify_token (Optional[str]): Token used for webhook verification
            challenges.
        app_secret (Optional[str]): Meta app secret for webhook signature
            verification.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the WhatsApp adapter with API credentials.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``phone_number_id`` (str): WhatsApp Business phone number ID.
                ``access_token`` (str): Meta Graph API access token.
                ``verify_token`` (str): Webhook verification token.
                ``app_secret`` (str): Meta app secret for signature
                    verification.
        """
        super().__init__(config)
        self.phone_number_id = config.get("phone_number_id")
        self.access_token = config.get("access_token")
        self.verify_token = config.get("verify_token")
        self.app_secret = config.get("app_secret")
        self._session = None
        self._api_url = f"https://graph.facebook.com/v18.0/{self.phone_number_id}"

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"whatsapp"``.
        """
        return "whatsapp"

    async def connect(self) -> None:
        """Initialize the WhatsApp Business API client and verify credentials.
        Creates an httpx session with the Meta Graph API authorization header
        and verifies credentials by fetching the phone number info.

        Raises:
            RuntimeError: If ``httpx`` is not installed or if the API
                verification call fails.
        """
        try:
            import httpx

            self._session = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )

            # Verify credentials by getting phone number info
            response = await self._session.get(
                f"{self._api_url}"
            )
            if response.status_code != 200:
                raise RuntimeError(f"WhatsApp API error: {response.text}")

            logger.info(f"Connected to WhatsApp Business API")

        except ImportError:
            raise RuntimeError(
                "httpx not installed. "
                "Install with: pip install httpx"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to WhatsApp: {e}")

    async def disconnect(self) -> None:
        """Disconnect the WhatsApp client and release resources.

        Closes the httpx session if one is open.
        """
        if self._session:
            await self._session.aclose()
        self._session = None
        logger.info("Disconnected from WhatsApp")

    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message to a WhatsApp user.

        Sends a ``text`` type message via the WhatsApp Business Cloud API.
        Adds a ``context`` field for replies.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number. A leading ``+`` is added
                automatically if missing.
            content (str): Text content to send.
            reply_to (Optional[str]): WhatsApp message ID to reply to.
                Sets the ``context.message_id`` field. Defaults to None.

        Returns:
            str: WhatsApp message ID of the sent message, or an empty string
                if the response does not include one.

        Raises:
            RuntimeError: If the client is not connected or the API returns
                an error.
            ValueError: If ``target.user_id`` is not set.
        """
        if not self._session:
            raise RuntimeError("WhatsApp client not connected")

        if not target.user_id:
            raise ValueError("No target user_id (phone number) provided")

        # WhatsApp requires phone numbers in format: +1234567890
        phone = target.user_id
        if not phone.startswith("+"):
            phone = f"+{phone}"

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": content},
        }

        # Handle reply (WhatsApp uses context for replies)
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        response = await self._session.post(
            f"{self._api_url}/messages",
            json=payload,
        )

        if response.status_code != 200:
            raise RuntimeError(f"Failed to send WhatsApp message: {response.text}")

        data = response.json()
        messages = data.get("messages", [])
        if messages:
            return messages[0].get("id", "")

        return ""

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send a media attachment to a WhatsApp user.

        Selects the WhatsApp media type (``image``, ``video``, ``audio``, or
        ``document``) based on ``media.mime_type``. Requires a public URL
        via ``media.url``; local file upload requires an external media server
        and is not implemented.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number.
            media (MediaAttachment): Media to send. ``url`` must be set.

        Returns:
            str: WhatsApp message ID of the sent message, or an empty string
                if the response does not include one.

        Raises:
            RuntimeError: If the client is not connected or the API returns
                an error.
            NotImplementedError: If ``media.file_path`` is set (local file
                upload requires a media server).
            ValueError: If ``target.user_id`` is not set, or if neither
                ``media.url`` nor ``media.file_path`` is provided.
        """
        if not self._session:
            raise RuntimeError("WhatsApp client not connected")

        if not target.user_id:
            raise ValueError("No target user_id (phone number) provided")

        phone = target.user_id
        if not phone.startswith("+"):
            phone = f"+{phone}"

        # Determine media type
        media_type = "image"
        if media.mime_type:
            if media.mime_type.startswith("video"):
                media_type = "video"
            elif media.mime_type.startswith("audio"):
                media_type = "audio"
            elif media.mime_type.startswith("application/pdf"):
                media_type = "document"

        # Use media URL or upload
        if media.url:
            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": media_type,
                media_type: {"link": media.url},
            }
            if media.caption:
                payload[media_type]["caption"] = media.caption
        elif media.file_path:
            # For local files, we need to upload to WhatsApp first
            # This is a simplified version - in production you'd upload to a server
            raise NotImplementedError(
                "Local file upload requires a media server. Use URL instead."
            )
        else:
            raise ValueError("No file_path or URL provided for media")

        response = await self._session.post(
            f"{self._api_url}/messages",
            json=payload,
        )

        if response.status_code != 200:
            raise RuntimeError(f"Failed to send WhatsApp media: {response.text}")

        data = response.json()
        messages = data.get("messages", [])
        if messages:
            return messages[0].get("id", "")

        return ""

    async def react(self, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to a WhatsApp message (not fully supported).

        The WhatsApp Business Cloud API does not support arbitrary emoji
        reactions.

        Args:
            message_id (str): Unused.
            emoji (str): Unused.

        Raises:
            NotImplementedError: Always — WhatsApp reactions are not fully
                supported in the Business API.
        """
        # WhatsApp doesn't support reactions in the same way
        # You can only react with limited emoji via the API
        raise NotImplementedError(
            "WhatsApp reactions not fully supported in Business API"
        )

    def verify_webhook(self, payload: str, signature: str) -> bool:
        """Verify a WhatsApp webhook signature using HMAC-SHA256.

        Computes the expected signature from the app secret and raw payload,
        then compares it to the provided signature using a constant-time
        comparison.

        Args:
            payload (str): Raw request body as a string.
            signature (str): Value of the ``X-Hub-Signature-256`` header
                (format: ``sha256=<hex>``).

        Returns:
            bool: ``True`` if the signature is valid, ``False`` otherwise or
                if no app secret is configured.
        """
        if not self.app_secret:
            return False

        # Calculate expected signature
        expected_signature = hmac.new(
            self.app_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(f"sha256={expected_signature}", signature)

    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Parse and handle an incoming WhatsApp Business webhook payload.

        Iterates over ``entry[].changes[].value.messages[]`` and returns the
        first text message found. Skips verification challenges and non-text
        message types.

        Args:
            payload (Dict[str, Any]): Raw JSON webhook payload from the Meta
                platform.

        Returns:
            Optional[Message]: Parsed message for the first text message
                found, or ``None`` if none are present or on parse error.
        """
        try:
            # Handle verification challenge
            if payload.get("object") == "whatsapp_business_account":
                # This is a webhook verification
                return None

            # Handle incoming messages
            entry = payload.get("entry", [])
            if not entry:
                return None

            for e in entry:
                changes = e.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])

                    for msg in messages:
                        # Skip non-text messages for now
                        if msg.get("type") != "text":
                            continue

                        text = msg.get("text", {})
                        content = text.get("body", "")

                        # Get sender info
                        metadata = value.get("metadata", {})
                        phone_number_id = metadata.get("phone_number_id", "")

                        return Message(
                            id=msg.get("id", ""),
                            channel="whatsapp",
                            sender=msg.get("from", ""),
                            sender_name=msg.get("from", ""),  # WhatsApp doesn't provide names
                            content=content,
                            timestamp=now(),
                            metadata={
                                "phone_number_id": phone_number_id,
                                "message_id": msg.get("id", ""),
                            },
                            reply_to=msg.get("context", {}).get("id"),
                        )

            return None

        except Exception as e:
            logger.error(f"Error handling WhatsApp webhook: {e}")
            return None

    async def _listen(self) -> None:
        """No-op listener for WhatsApp.

        WhatsApp Business Cloud API delivers messages via webhooks. Polling
        is not supported and this method is a no-op placeholder.
        """
        pass
