"""Signal channel adapter."""
import asyncio
import logging
import json
import subprocess
from typing import Any, Dict, Optional, List
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.signal")


class SignalAdapter(ChannelAdapter):
    """
    Signal messenger adapter using signal-cli.
    
    Requires signal-cli to be installed and configured.
    Install: https://github.com/AsamK/signal-cli
    
    Can use either:
    1. signal-cli daemon mode (REST API)
    2. subprocess calls (simpler but slower)
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.phone_number = config.get("phone_number")
        self.signal_cli_path = config.get("signal_cli_path", "signal-cli")
        self.use_daemon = config.get("use_daemon", False)
        self.daemon_url = config.get("daemon_url", "http://localhost:8080")
        self._session = None
        self._process: Optional[asyncio.subprocess.Process] = None

    @property
    def channel_name(self) -> str:
        return "signal"

    async def connect(self) -> None:
        """Initialize the Signal client."""
        try:
            import httpx

            if self.use_daemon:
                # Connect to daemon
                self._session = httpx.AsyncClient(
                    base_url=self.daemon_url,
                    timeout=30.0,
                )
                # Test connection
                response = await self._session.get("/v1/health")
                if response.status_code != 200:
                    raise RuntimeError(f"Signal daemon not responding: {response.text}")
            else:
                # Verify signal-cli is available
                result = await asyncio.create_subprocess_exec(
                    self.signal_cli_path, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await result.communicate()
                if result.returncode != 0:
                    raise RuntimeError("signal-cli not found")

            logger.info(f"Connected to Signal")

        except ImportError:
            raise RuntimeError(
                "httpx not installed. "
                "Install with: pip install httpx"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Signal: {e}")

    async def disconnect(self) -> None:
        """Disconnect the Signal client."""
        if self._session:
            await self._session.aclose()
        self._session = None

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

        logger.info("Disconnected from Signal")

    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a Signal user."""
        if not target.user_id:
            raise ValueError("No target user_id (phone number) provided")

        # Normalize phone number
        phone = target.user_id
        if not phone.startswith("+"):
            phone = f"+{phone}"

        if self.use_daemon and self._session:
            # Use daemon API
            payload = {
                "recipients": [phone],
                "message": content,
            }

            if reply_to:
                payload["quote"] = {"timestamp": reply_to, "author": self.phone_number}

            response = await self._session.post(
                "/v1/send",
                json=payload,
            )

            if response.status_code not in (200, 201):
                raise RuntimeError(f"Failed to send Signal message: {response.text}")

            data = response.json()
            return data.get("timestamp", "")
        else:
            # Use subprocess
            args = [
                self.signal_cli_path,
                "-u", self.phone_number,
                "send",
                "-m", content,
            ]

            if reply_to:
                args.extend(["--quote", str(reply_to)])

            args.append(phone)

            result = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                raise RuntimeError(f"Signal send failed: {stderr.decode()}")

            # Return timestamp as message ID (approximation)
            return str(int(datetime.now().timestamp() * 1000))

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media to a Signal user."""
        if not target.user_id:
            raise ValueError("No target user_id (phone number) provided")

        phone = target.user_id
        if not phone.startswith("+"):
            phone = f"+{phone}"

        # Determine attachment type
        attachments: List[str] = []

        if media.file_path:
            attachments.append(media.file_path)
        elif media.url:
            # Download URL first (simplified)
            import tempfile
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.get(media.url)
                with tempfile.NamedTemporaryFile(delete=False) as f:
                    f.write(response.content)
                    attachments.append(f.name)
        else:
            raise ValueError("No file_path or URL provided for media")

        if self.use_daemon and self._session:
            # Use daemon API with multipart
            files = {"attachment": open(attachments[0], "rb")}
            data = {
                "recipients": phone,
                "message": media.caption or "",
            }

            response = await self._session.post(
                "/v1/send",
                files=files,
                data=data,
            )

            if response.status_code not in (200, 201):
                raise RuntimeError(f"Failed to send Signal media: {response.text}")

            return str(int(datetime.now().timestamp() * 1000))
        else:
            # Use subprocess
            args = [
                self.signal_cli_path,
                "-u", self.phone_number,
                "send",
                "-m", media.caption or "",
                "--attachment", attachments[0],
                phone,
            ]

            result = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                raise RuntimeError(f"Signal send failed: {stderr.decode()}")

            return str(int(datetime.now().timestamp() * 1000))

    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        if self.use_daemon and self._session:
            # Use daemon API
            payload = {
                "recipient": self.phone_number,
                "reaction": {
                    "emoji": emoji,
                    "targetAuthor": message_id.split("_")[0] if "_" in message_id else "",
                    "targetTimestamp": message_id.split("_")[1] if "_" in message_id else message_id,
                }
            }

            response = await self._session.post(
                "/v1/reactions",
                json=payload,
            )

            if response.status_code not in (200, 201):
                logger.warning(f"Failed to send Signal reaction: {response.text}")
        else:
            # Use subprocess
            args = [
                self.signal_cli_path,
                "-u", self.phone_number,
                "react",
                "-e", emoji,
                "-m", message_id,
            ]

            result = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.warning(f"Failed to send Signal reaction: {stderr.decode()}")

    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming Signal message (from daemon)."""
        try:
            # Handle envelope
            envelope = payload

            if envelope.get("type") != "dataMessage":
                return None

            data = envelope.get("dataMessage", {})
            if not data:
                return None

            # Skip if from self
            if data.get("sender") == self.phone_number:
                return None

            # Get message text
            content = ""
            if data.get("message"):
                content = data.get("message", "")

            # Get attachments
            attachments = data.get("attachments", [])
            if attachments and not content:
                content = f"[Attachment: {attachments[0].get('filename', 'file')}]"

            # Get sender
            sender = data.get("sender", "")
            sender_name = sender  # Signal doesn't provide display names

            # Get timestamp
            timestamp = datetime.fromtimestamp(
                int(data.get("timestamp", 0)) / 1000
            )

            # Get reaction info if applicable
            reaction = data.get("reaction", {})

            return Message(
                id=f"{sender}_{data.get('timestamp', '')}",
                channel="signal",
                sender=sender,
                sender_name=sender_name,
                content=content,
                timestamp=timestamp,
                metadata={
                    "sender": sender,
                    "timestamp": data.get("timestamp"),
                    "reaction": reaction,
                },
            )

        except Exception as e:
            logger.error(f"Error handling Signal webhook: {e}")
            return None

    async def _listen(self) -> None:
        """Signal uses daemon or subprocess, polling not needed."""
        pass
