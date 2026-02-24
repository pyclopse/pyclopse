"""WhatsApp channel adapter using WhatsApp Business Cloud API."""
import asyncio
import logging
import hashlib
import hmac
import base64
from typing import Any, Dict, Optional
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.whatsapp")


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp Business Cloud API adapter."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.phone_number_id = config.get("phone_number_id")
        self.access_token = config.get("access_token")
        self.verify_token = config.get("verify_token")
        self.app_secret = config.get("app_secret")
        self._session = None
        self._api_url = f"https://graph.facebook.com/v18.0/{self.phone_number_id}"

    @property
    def channel_name(self) -> str:
        return "whatsapp"

    async def connect(self) -> None:
        """Initialize the WhatsApp client."""
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
        """Disconnect the WhatsApp client."""
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
        """Send a message to a WhatsApp user."""
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
        """Send media to a WhatsApp user."""
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
        """Add reaction to a message."""
        # WhatsApp doesn't support reactions in the same way
        # You can only react with limited emoji via the API
        raise NotImplementedError(
            "WhatsApp reactions not fully supported in Business API"
        )

    def verify_webhook(self, payload: str, signature: str) -> bool:
        """Verify WhatsApp webhook signature."""
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
        """Handle incoming WhatsApp webhook."""
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
                            timestamp=datetime.now(),
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
        """WhatsApp uses webhooks, polling not needed."""
        pass
