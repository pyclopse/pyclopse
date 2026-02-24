"""iMessage channel adapter."""
import asyncio
import logging
import subprocess
from typing import Any, Dict, Optional, List
from datetime import datetime

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.imessage")


class IMessageAdapter(ChannelAdapter):
    """
    Apple iMessage adapter using imsg CLI or bluebubbles.
    
    Requires one of:
    1. imsg CLI installed (https://github.com/jakewatkins/imsg)
    2. BlueBubbles server (https://bluebubbles.app)
    
    This adapter works on macOS and requires the Messages app.
    """

    def __init__(self, config: Dict[str, Any]):
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
        return "imessage"
    
    async def connect(self) -> None:
        """Initialize the iMessage client."""
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
        """Disconnect from iMessage."""
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
        """Send a message to an iMessage recipient."""
        to = target.user_id
        if not to:
            raise ValueError("No target user_id provided")
        
        # Handle BlueBubbles API
        if self.use_bluebubbles:
            return await self._send_via_bluebubbles(to, content, reply_to)
        
        # Handle imsg CLI
        return await self._send_via_imsg(to, content, reply_to)
    
    async def _send_viasg(self, to_im: str, content: str, reply_to: Optional[str] = None) -> str:
        """Send message via imsg CLI."""
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
        """Send message via BlueBubbles API."""
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
        """Send media via iMessage."""
        to = target.user_id
        if not to:
            raise ValueError("No target user_id provided")
        
        if self.use_bluebubbles:
            return await self._send_media_bluebubbles(to, media)
        
        # imsg can send attachments
        return await self._send_media_imsg(to, media)
    
    async def _send_media_imsg(self, to: str, media: MediaAttachment) -> str:
        """Send media via imsg CLI."""
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
        """Send media via BlueBubbles API."""
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
        """Add reaction to a message."""
        # iMessage reactions are complex - would need to use AppleScript or bluebubbles
        logger.debug(f"iMessage reactions not fully implemented, ignoring: {emoji}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming webhook (from BlueBubbles)."""
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
                timestamp=datetime.fromisoformat(msg.get("date", datetime.now().isoformat())),
                metadata={
                    "service": msg.get("service"),
                    "chat_guid": msg.get("chatGuid"),
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling iMessage webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Listen for messages (polling or long-poll)."""
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
