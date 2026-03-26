"""Signal channel adapter."""
import asyncio
import logging
import json
import subprocess
from typing import Any, Dict, Optional, List
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclopse.channels.signal")


class SignalAdapter(ChannelAdapter):
    """Signal messenger adapter using signal-cli.

    Supports two backends:

    1. **Daemon mode** — connects to a running ``signal-cli`` REST API daemon
       (faster, recommended for production).
    2. **Subprocess mode** — spawns ``signal-cli`` directly for each
       operation (simpler but slower).

    Requires ``signal-cli`` to be installed and a Signal account registered.
    See: https://github.com/AsamK/signal-cli

    Attributes:
        phone_number (Optional[str]): Registered Signal phone number in
            E.164 format (e.g. ``"+15551234567"``).
        signal_cli_path (str): Path to the ``signal-cli`` binary. Defaults
            to ``"signal-cli"``.
        use_daemon (bool): Whether to use the daemon REST API. Defaults to
            ``False``.
        daemon_url (str): Base URL of the signal-cli daemon REST API.
            Defaults to ``"http://localhost:8080"``.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the Signal adapter with backend configuration.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``phone_number`` (str): Registered Signal phone number.
                ``signal_cli_path`` (str): Path to signal-cli binary.
                    Defaults to ``"signal-cli"``.
                ``use_daemon`` (bool): Use daemon REST API. Defaults to
                    ``False``.
                ``daemon_url`` (str): Daemon base URL. Defaults to
                    ``"http://localhost:8080"``.
        """
        super().__init__(config)
        self.phone_number = config.get("phone_number")
        self.signal_cli_path = config.get("signal_cli_path", "signal-cli")
        self.use_daemon = config.get("use_daemon", False)
        self.daemon_url = config.get("daemon_url", "http://localhost:8080")
        self._session = None
        self._process: Optional[asyncio.subprocess.Process] = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"signal"``.
        """
        return "signal"

    async def connect(self) -> None:
        """Initialize the Signal backend connection.
        In daemon mode, creates an httpx session and checks the daemon
        health endpoint. In subprocess mode, verifies the ``signal-cli``
        binary is available by running ``signal-cli --version``.

        Raises:
            RuntimeError: If ``httpx`` is not installed, the daemon is not
                reachable, or ``signal-cli`` is not found.
        """
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
        """Disconnect the Signal client and release resources.

        Closes the httpx session if in daemon mode, and terminates the
        subprocess if one is running. Waits up to 5 seconds for the process
        to exit before forcibly killing it.
        """
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
        """Send a text message to a Signal user.

        Uses the daemon REST API when ``use_daemon`` is ``True``, otherwise
        invokes ``signal-cli`` in a subprocess.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number in E.164 format. A leading ``+``
                is added automatically if missing.
            content (str): Text content to send.
            reply_to (Optional[str]): Message timestamp to quote. Defaults to
                None.

        Returns:
            str: Message timestamp returned by the daemon, or a millisecond
                epoch timestamp string in subprocess mode.

        Raises:
            ValueError: If ``target.user_id`` is not set.
            RuntimeError: If the daemon returns an error or ``signal-cli``
                exits with a non-zero code.
        """
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
        """Send a media attachment to a Signal user.

        Downloads URL-based media to a temporary file before sending.
        Uses the daemon multipart API or ``signal-cli --attachment``.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number.
            media (MediaAttachment): Media to send. Either ``file_path`` or
                ``url`` must be set.

        Returns:
            str: Millisecond epoch timestamp string representing the send
                time (approximation).

        Raises:
            ValueError: If ``target.user_id`` is not set, or if neither
                ``media.file_path`` nor ``media.url`` is provided.
            RuntimeError: If the daemon returns an error or ``signal-cli``
                exits with a non-zero code.
        """
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
        """Add an emoji reaction to a Signal message.

        Uses the daemon ``/v1/reactions`` endpoint in daemon mode, or the
        ``signal-cli react`` subcommand in subprocess mode. The
        ``message_id`` is expected to be in the format
        ``{author_phone}_{timestamp}`` for daemon mode.

        Args:
            message_id (str): Compound ID in the format
                ``{author}_{timestamp}`` for daemon mode, or just a timestamp
                for subprocess mode.
            emoji (str): Unicode emoji character to react with.
        """
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
        """Parse and handle an incoming Signal message from the daemon.

        Expects a signal-cli daemon envelope payload with ``type`` set to
        ``"dataMessage"``. Skips messages sent from the bot's own number.
        Falls back to an attachment description if no text is present.

        Args:
            payload (Dict[str, Any]): Raw JSON envelope payload delivered by
                the signal-cli daemon.

        Returns:
            Optional[Message]: Parsed message, or ``None`` if the envelope
                is not a data message, is from self, or cannot be parsed.
        """
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
        """No-op listener for Signal.

        Signal message delivery uses the daemon webhook or subprocess mode.
        Polling is not needed and this method is a no-op placeholder.
        """
        pass
