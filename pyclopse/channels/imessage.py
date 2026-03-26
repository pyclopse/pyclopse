"""iMessage channel adapter."""
import asyncio
import logging
import subprocess
from typing import Any, Dict, Optional, List
from datetime import datetime
from pyclopse.utils.time import now

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclopse.channels.imessage")


class IMessageAdapter(ChannelAdapter):
    """Apple iMessage adapter using the imsg CLI or a BlueBubbles server.

    Requires one of:

    1. ``imsg`` CLI installed — https://github.com/jakewatkins/imsg
    2. BlueBubbles server running — https://bluebubbles.app

    This adapter only works on macOS and requires the Messages app to be
    configured with an Apple ID.

    Attributes:
        imsg_path (str): Path to the ``imsg`` binary. Defaults to ``"imsg"``.
        db_path (Optional[str]): Path to the Messages ``chat.db`` SQLite file.
        service (str): Service type, e.g. ``"iMessage"`` or ``"SMS"``.
        region (str): Region code. Defaults to ``"US"``.
        use_bluebubbles (bool): Whether to use BlueBubbles instead of imsg.
        bluebubbles_url (str): Base URL of the BlueBubbles server.
        bluebubbles_api_key (Optional[str]): API key for BlueBubbles auth.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the iMessage adapter with backend configuration.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``imsg_path`` (str): Path to imsg binary. Defaults to
                    ``"imsg"``.
                ``db_path`` (str): Path to Messages chat.db.
                ``service`` (str): Service type. Defaults to ``"iMessage"``.
                ``region`` (str): Region code. Defaults to ``"US"``.
                ``use_bluebubbles`` (bool): Use BlueBubbles API instead of
                    imsg. Defaults to ``False``.
                ``bluebubbles_url`` (str): BlueBubbles server URL. Defaults to
                    ``"http://localhost:1234"``.
                ``bluebubbles_api_key`` (str): BlueBubbles API key.
        """
        super().__init__(config)
        self.imsg_path = config.get("imsg_path", "imsg")
        self.db_path = config.get("db_path")  # Path to chat.db
        self.service = config.get("service", "iMessage")  # iMessage, SMS, etc.
        self.region = config.get("region", "US")
        self.use_bluebubbles = config.get("use_bluebubbles", False)
        self.bluebubbles_url = config.get("bluebubbles_url", "http://localhost:1234")
        self.bluebubbles_api_key = config.get("bluebubbles_api_key")
        self._session = None

    @property
    def channel_name(self) -> str:
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"imessage"``.
        """
        return "imessage"

    async def connect(self) -> None:
        """Initialize the iMessage backend connection.
        When ``use_bluebubbles`` is ``True``, creates an httpx session and
        verifies the BlueBubbles server health endpoint. Otherwise, verifies
        the ``imsg`` binary is available.

        Raises:
            RuntimeError: If ``httpx`` is not installed, the BlueBubbles
                server is not responding, or ``imsg`` is not found.
        """
        try:
            if self.use_bluebubbles:
                import httpx
                self._session = httpx.AsyncClient(
                    base_url=self.bluebubbles_url,
                    headers={"Authorization": f"Bearer {self.bluebubbles_api_key}"},
                    timeout=30.0,
                )
                # Test connection
                response = await self._session.get("/api/v1/health")
                if response.status_code != 200:
                    raise RuntimeError(f"BlueBubbles not responding: {response.text}")
            else:
                # Verify imsg is available
                result = await asyncio.create_subprocess_exec(
                    self.imsg_path, "version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await result.communicate()
                if result.returncode != 0:
                    raise RuntimeError("imsg not found. Install from https://github.com/jakewatkins/imsg")

            logger.info(f"Connected to iMessage")

        except ImportError:
            raise RuntimeError(
                "httpx not installed. "
                "Install with: pip install httpx"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to iMessage: {e}")

    async def disconnect(self) -> None:
        """Disconnect from iMessage and release resources.

        Closes the BlueBubbles httpx session if one is open.
        """
        if self._session:
            await self._session.aclose()
        self._session = None
        logger.info("Disconnected from iMessage")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message to an iMessage recipient.

        Delegates to either :meth:`_send_via_bluebubbles` or
        :meth:`_send_via_imsg` depending on the configured backend.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number or email address.
            content (str): Text content to send.
            reply_to (Optional[str]): Message GUID to quote/reply to.
                Defaults to None.

        Returns:
            str: Message GUID returned by the backend, or ``"sent"`` if
                unavailable.

        Raises:
            ValueError: If ``target.user_id`` is not set.
        """
        to = target.user_id
        if not to:
            raise ValueError("No target user_id provided")
        
        # Handle BlueBubbles API
        if self.use_bluebubbles:
            return await self._send_via_bluebubbles(to, content, reply_to)
        
        # Handle imsg CLI
        return await self._send_via_imsg(to, content, reply_to)
    
    async def _send_via_imsg(self, to: str, content: str, reply_to: Optional[str] = None) -> str:
        """Send a text message via the imsg CLI.

        Spawns an imsg subprocess, passes the content via stdin, and returns
        the message GUID printed to stdout on success.

        Args:
            to (str): Recipient phone number or email address.
            content (str): Text content to send.
            reply_to (Optional[str]): Unused — imsg does not support quoting.
                Defaults to None.

        Returns:
            str: Message GUID from imsg output, or ``"sent"`` if no output.

        Raises:
            RuntimeError: If imsg exits with a non-zero return code.
            RuntimeError: If the imsg binary is not found.
        """
        cmd = [self.imsg_path, "send", "-"]
        
        if self.db_path:
            cmd.extend(["-d", self.db_path])
        
        if self.service != "iMessage":
            cmd.extend(["-s", self.service])
        
        cmd.append(to)
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await process.communicate(input=content.encode())
            
            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                raise RuntimeError(f"imsg failed: {error_msg}")
            
            # Parse output for message ID
            output = stdout.decode() if stdout else ""
            # imsg returns the message GUID on success
            return output.strip() or "sent"
            
        except FileNotFoundError:
            raise RuntimeError("imsg not found. Install from https://github.com/jakewatkins/imsg")
    
    async def _send_via_bluebubbles(self, to: str, content: str, reply_to: Optional[str] = None) -> str:
        """Send a text message via the BlueBubbles REST API.

        Args:
            to (str): Recipient phone number or email address.
            content (str): Text content to send.
            reply_to (Optional[str]): Message GUID to quote in the reply.
                Defaults to None.

        Returns:
            str: Message GUID returned by BlueBubbles, or ``"sent"`` if
                absent from the response.

        Raises:
            RuntimeError: If the BlueBubbles client is not connected or if
                the API returns a non-200 status.
        """
        if not self._session:
            raise RuntimeError("BlueBubbles client not connected")
        
        payload = {
            "address": to,
            "text": content,
            "service": "iMessage" if self.service == "iMessage" else "sms",
        }
        
        if reply_to:
            payload["replyToGuid"] = reply_to
        
        response = await self._session.post("/api/v1/message/send", json=payload)
        
        if response.status_code != 200:
            raise RuntimeError(f"BlueBubbles API error: {response.text}")
        
        result = response.json()
        return result.get("guid", "sent")
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send a media attachment via iMessage.

        Delegates to :meth:`_send_media_bluebubbles` or
        :meth:`_send_media_imsg` depending on the configured backend.

        Args:
            target (MessageTarget): Destination. Uses ``target.user_id`` as
                the recipient phone number or email address.
            media (MediaAttachment): Media to send. ``file_path`` must be set.

        Returns:
            str: Message GUID or ``"sent"``.

        Raises:
            ValueError: If ``target.user_id`` is not set.
        """
        to = target.user_id
        if not to:
            raise ValueError("No target user_id provided")
        
        if self.use_bluebubbles:
            return await self._send_media_bluebubbles(to, media)
        
        # imsg can send attachments
        return await self._send_media_imsg(to, media)
    
    async def _send_media_imsg(self, to: str, media: MediaAttachment) -> str:
        """Send a media attachment via the imsg CLI using the ``-a`` flag.

        Args:
            to (str): Recipient phone number or email address.
            media (MediaAttachment): Media to send. ``file_path`` must be set.
                URL media is not supported and raises ``NotImplementedError``.

        Returns:
            str: Always ``"sent"`` on success.

        Raises:
            NotImplementedError: If only ``media.url`` is provided.
            RuntimeError: If imsg exits with a non-zero return code or is not
                found.
        """
        file_path = media.file_path
        if not file_path and media.url:
            # Would need to download first
            raise NotImplementedError("URL media not yet supported, use file_path")
        
        cmd = [self.imsg_path, "send", "-a", file_path]
        
        if self.db_path:
            cmd.extend(["-d", self.db_path])
        
        cmd.append(to)
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                raise RuntimeError(f"imsg failed: {error_msg}")
            
            return "sent"
            
        except FileNotFoundError:
            raise RuntimeError("imsg not found")
    
    async def _send_media_bluebubbles(self, to: str, media: MediaAttachment) -> str:
        """Send a media attachment via the BlueBubbles REST API.

        Uploads the file as a multipart form POST and includes an optional
        caption as message text.

        Args:
            to (str): Recipient phone number or email address.
            media (MediaAttachment): Media to send. ``file_path`` must be set.

        Returns:
            str: Message GUID from BlueBubbles, or ``"sent"`` if absent.

        Raises:
            RuntimeError: If the BlueBubbles client is not connected or if
                the API returns a non-200 status.
        """
        if not self._session:
            raise RuntimeError("BlueBubbles client not connected")
        
        # BlueBubbles supports sending attachments
        files = {}
        if media.file_path:
            with open(media.file_path, "rb") as f:
                files["file"] = (media.file_path.split("/")[-1], f.read())
        
        data = {
            "address": to,
            "text": media.caption or "",
            "service": "iMessage",
        }
        
        response = await self._session.post(
            "/api/v1/message/send",
            data=data,
            files=files if files else None,
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"BlueBubbles API error: {response.text}")
        
        result = response.json()
        return result.get("guid", "sent")
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to an iMessage message (not fully implemented).

        iMessage reactions require AppleScript or BlueBubbles and are not
        currently implemented. The call is silently ignored with a debug log.

        Args:
            message_id (str): Message GUID to react to.
            emoji (str): Emoji to use as the reaction.
        """
        # iMessage reactions are complex - would need to use AppleScript or bluebubbles
        logger.debug(f"iMessage reactions not fully implemented, ignoring: {emoji}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Parse and handle an incoming BlueBubbles webhook payload.

        BlueBubbles delivers incoming iMessages as webhook POSTs. The payload
        may have a nested ``data.message`` structure or be flat.

        Args:
            payload (Dict[str, Any]): Raw JSON webhook payload from
                BlueBubbles.

        Returns:
            Optional[Message]: Parsed message, or ``None`` on parse error.
        """
        try:
            # BlueBubbles sends messages as webhook
            if "data" in payload:
                data = payload.get("data", {})
            else:
                data = payload
            
            msg = data.get("message", data)
            
            return Message(
                id=msg.get("guid", ""),
                channel="imessage",
                sender=msg.get("sender", ""),
                sender_name=msg.get("senderName", "iMessage User"),
                content=msg.get("text", ""),
                timestamp=datetime.fromisoformat(msg.get("date", now().isoformat())),
                metadata={
                    "service": msg.get("service"),
                    "chat_guid": msg.get("chatGuid"),
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling iMessage webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Poll for new iMessages via BlueBubbles or sleep as a no-op fallback.

        When ``use_bluebubbles`` is ``True``, polls the BlueBubbles
        ``/api/v1/chat/latest`` endpoint with a 60-second timeout. Otherwise,
        sleeps in a loop since imsg does not support listening. In production,
        configure BlueBubbles webhooks for real-time delivery instead.
        """
        if self.use_bluebubbles:
            # BlueBubbles supports long-polling
            while self._running:
                try:
                    response = await self._session.get(
                        "/api/v1/chat/latest",
                        timeout=60.0,
                    )
                    if response.status_code == 200:
                        # Process new messages
                        pass
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error polling iMessage: {e}")
                    await asyncio.sleep(5)
        else:
            # imsg doesn't support listening - would need to poll database
            while self._running:
                await asyncio.sleep(30)
