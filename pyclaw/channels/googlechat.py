"""Google Chat channel adapter."""
import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from urllib.parse import urljoin

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.googlechat")


class GoogleChatAdapter(ChannelAdapter):
    """
    Google Chat adapter using Google Chat REST API.
    
    Requires:
    - Service account JSON credentials OR
    - OAuth2 tokens (for user-level access)
    - Google Chat API enabled in Google Cloud Console
    
    Docs: https://developers.google.com/hangouts/chat
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.service_account_json = config.get("service_account_json")
        self.service_account_file = config.get("service_account_file")
        self.access_token = config.get("access_token")
        self.refresh_token = config.get("refresh_token")
        self.client_id = config.get("client_id")
        self.client_secret = config.get("client_secret")
        self.bot_user = config.get("bot_user")  # Bot's user ID
        self._session = None
        self._token = None
    
    @property
    def channel_name(self) -> str:
        return "googlechat"
    
    async def connect(self) -> None:
        """Initialize the Google Chat API client."""
        try:
            import httpx
            
            # Get access token
            if self.service_account_json:
                self._token = await self._get_service_account_token()
            elif self.access_token:
                self._token = self.access_token
            
            self._session = httpx.AsyncClient(
                base_url="https://chat.googleapis.com/v1",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            
            # Verify by getting bot info
            if self.bot_user:
                response = await self._session.get(f"/users/{self.bot_user}")
                if response.status_code != 200:
                    raise RuntimeError(f"Google Chat API error: {response.text}")
                
                user_info = response.json()
                logger.info(f"Connected to Google Chat as {user_info.get('name', 'bot')}")
            else:
                logger.info("Connected to Google Chat")
            
        except ImportError:
            raise RuntimeError(
                "httpx not installed. "
                "Install with: pip install httpx"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Google Chat: {e}")
    
    async def _get_service_account_token(self) -> str:
        """Get access token from service account."""
        try:
            import jwt
            from google.auth import transport
        except ImportError:
            raise RuntimeError(
                "google-auth and PyJWT required for service account. "
                "Install with: pip install google-auth PyJWT"
            )
        
        # Load service account
        import json
        if self.service_account_json:
            if isinstance(self.service_account_json, str):
                service_data = json.loads(self.service_account_json)
            else:
                service_data = self.service_account_json
        elif self.service_account_file:
            with open(self.service_account_file) as f:
                service_data = json.load(f)
        else:
            raise ValueError("No service account credentials provided")
        
        now = int(datetime.now().timestamp())
        
        # Create JWT
        claim = {
            "iss": service_data["client_email"],
            "sub": service_data.get("delegated_email", service_data["client_email"]),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
            "scope": "https://www.googleapis.com/auth/chat.bot",
        }
        
        # Sign JWT
        import base64
        import hashlib
        
        # Create signing key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        
        private_key = serialization.load_pem_private_key(
            service_data["private_key"].encode(),
            password=None,
            backend=default_backend()
        )
        
        # Encode header
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        
        # Encode claim
        claim_b64 = base64.urlsafe_b64encode(
            json.dumps(claim).encode()
        ).rstrip(b"=").decode()
        
        # Sign
        signature = private_key.sign(
            f"{header}.{claim_b64}".encode(),
            padding.PKCS7v1_5(),
            hashes.SHA256()
        )
        
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        
        jwt_token = f"{header}.{claim_b64}.{sig_b64}"
        
        # Exchange for access token
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt_token,
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, data=token_data)
            if response.status_code != 200:
                raise RuntimeError(f"Token exchange failed: {response.text}")
            
            token_result = response.json()
            return token_result["access_token"]
    
    async def disconnect(self) -> None:
        """Disconnect from Google Chat."""
        if self._session:
            await self._session.aclose()
        self._session = None
        self._token = None
        logger.info("Disconnected from Google Chat")
    
    async def send_message(
        self,
        target: MessageTarget,
        content: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to a Google Chat space or user."""
        if not self._session:
            raise RuntimeError("Google Chat client not connected")
        
        # Determine target
        space_name = None
        user_name = None
        
        if target.group_id:
            # Space (room) - format: spaces/xxx
            space_name = target.group_id
            if not space_name.startswith("spaces/"):
                space_name = f"spaces/{space_name}"
        elif target.user_id:
            # User (DM) - format: users/xxx
            user_name = target.user_id
            if not user_name.startswith("users/"):
                user_name = f"users/{user_name}"
        else:
            raise ValueError("No target user_id or group_id provided")
        
        # Build message
        message = {"text": content}
        
        if reply_to:
            # Thread reply
            message["thread"] = {"threadKey": reply_to}
        
        # Send to space or DM
        if space_name:
            endpoint = f"/{space_name}/messages"
        else:
            endpoint = f"/{user_name}/messages"
        
        response = await self._session.post(endpoint, json=message)
        
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Google Chat API error: {response.text}")
        
        result = response.json()
        return result.get("name", "sent")
    
    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
    ) -> str:
        """Send media to Google Chat."""
        if not self._session:
            raise RuntimeError("Google Chat client not connected")
        
        # Google Chat media upload is complex - requires upload protocol
        # For now, send as text with link
        if media.url:
            content = f"{media.caption or 'Image'}\n{media.url}"
        elif media.file_path:
            raise NotImplementedError(
                "File upload requires Google Chat upload API. "
                "Use media URL instead."
            )
        else:
            raise ValueError("No media URL provided")
        
        # Send as regular message
        target_with_content = MessageTarget(
            channel=target.channel,
            user_id=target.user_id,
            group_id=target.group_id,
        )
        return await self.send_message(target_with_content, content)
    
    async def react(self, message_id: str, emoji: str) -> None:
        """Add reaction to a message."""
        if not self._session:
            raise RuntimeError("Google Chat client not connected")
        
        # Parse message_id to get space and message
        # Format: spaces/xxx/messages/yyy
        parts = message_id.split("/")
        if len(parts) < 4:
            logger.warning(f"Invalid message ID format: {message_id}")
            return
        
        space = "/".join(parts[:2])
        message_name = "/".join(parts[2:])
        
        # Add reaction
        reaction = {
            "emoji": {"emoji": emoji}
        }
        
        response = await self._session.post(
            f"{space}/reactions",
            json=reaction,
        )
        
        if response.status_code not in (200, 201):
            logger.warning(f"Failed to add reaction: {response.text}")
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Optional[Message]:
        """Handle incoming Google Chat webhook."""
        try:
            event_type = payload.get("type")
            
            if event_type == "MESSAGE":
                message = payload.get("message", {})
                sender = message.get("sender", {})
                
                # Extract thread info
                thread = message.get("thread", {})
                
                return Message(
                    id=message.get("name", ""),
                    channel="googlechat",
                    sender=sender.get("name", ""),
                    sender_name=sender.get("displayName", "Google Chat User"),
                    content=message.get("argumentText", message.get("text", "")),
                    timestamp=datetime.now(),
                    metadata={
                        "space": payload.get("space", {}).get("name"),
                        "thread_key": thread.get("threadKey"),
                        "thread_name": thread.get("name"),
                    },
                )
            
            elif event_type == "ADDED_TO_SPACE":
                return Message(
                    id=f"added-{payload.get('timestamp', '')}",
                    channel="googlechat",
                    sender=payload.get("user", {}).get("name", ""),
                    sender_name=payload.get("user", {}).get("displayName", "Google Chat User"),
                    content="[added_to_space]",
                    timestamp=datetime.now(),
                    metadata={"space": payload.get("space", {}).get("name")},
                )
            
            elif event_type == "REMOVED_FROM_SPACE":
                return Message(
                    id=f"removed-{payload.get('timestamp', '')}",
                    channel="googlechat",
                    sender=payload.get("user", {}).get("name", ""),
                    sender_name=payload.get("user", {}).get("displayName", "Google Chat User"),
                    content="[removed_from_space]",
                    timestamp=datetime.now(),
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error handling Google Chat webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Listen for messages (polling fallback)."""
        # Google Chat primarily uses webhooks, but we can poll for new messages
        # This requires the spaces.list and spaces.messages.list APIs
        while self._running:
            await asyncio.sleep(30)
