"""Google Chat channel adapter."""
import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from pyclaw.utils.time import now
from urllib.parse import urljoin

from .base import ChannelAdapter, Message, MessageTarget, MediaAttachment

logger = logging.getLogger("pyclaw.channels.googlechat")


class GoogleChatAdapter(ChannelAdapter):
    """Google Chat adapter using the Google Chat REST API.

    Supports service account credentials or direct OAuth2 access tokens for
    authentication. Requires the Google Chat API to be enabled in Google Cloud
    Console and ``httpx`` installed.

    See: https://developers.google.com/hangouts/chat

    Attributes:
        service_account_json (Optional[str | dict]): Service account JSON
            as a string or dict, used for server-to-server auth.
        service_account_file (Optional[str]): Path to a service account JSON
            file, used when ``service_account_json`` is not provided.
        access_token (Optional[str]): Pre-obtained OAuth2 access token.
        refresh_token (Optional[str]): OAuth2 refresh token for token renewal.
        client_id (Optional[str]): OAuth2 client ID.
        client_secret (Optional[str]): OAuth2 client secret.
        bot_user (Optional[str]): Bot's user resource name for verification.
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize the Google Chat adapter with API credentials.

        Args:
            config (Dict[str, Any]): Configuration dictionary. Expected keys:
                ``service_account_json`` (str | dict): Service account JSON.
                ``service_account_file`` (str): Path to service account file.
                ``access_token`` (str): Pre-obtained OAuth2 access token.
                ``refresh_token`` (str): OAuth2 refresh token.
                ``client_id`` (str): OAuth2 client ID.
                ``client_secret`` (str): OAuth2 client secret.
                ``bot_user`` (str): Bot user resource name for verification.
        """
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
        """Return the channel name for this adapter.

        Returns:
            str: Always ``"googlechat"``.
        """
        return "googlechat"

    async def connect(self) -> None:
        """Initialize the Google Chat API client and verify credentials.
        Obtains an access token via service account or uses the configured
        ``access_token`` directly. Creates an ``httpx.AsyncClient`` and
        optionally verifies credentials by fetching the bot user profile.

        Raises:
            RuntimeError: If ``httpx`` is not installed or if authentication
                or connection fails.
        """
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
        """Obtain a Google OAuth2 access token from service account credentials.

        Constructs and signs a JWT using the service account private key, then
        exchanges it for an access token at the Google token endpoint.

        Returns:
            str: A short-lived OAuth2 access token valid for one hour.

        Raises:
            RuntimeError: If ``google-auth`` or ``PyJWT`` are not installed.
            ValueError: If no service account credentials are provided.
            RuntimeError: If the token exchange request fails.
        """
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
        
        now = int(now().timestamp())
        
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
        """Disconnect from Google Chat and release resources.

        Closes the httpx session and clears the stored access token.
        """
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
        """Send a text message to a Google Chat space or user.

        Routes to the appropriate Google Chat API endpoint based on whether
        ``target.group_id`` (space) or ``target.user_id`` (DM) is set.

        Args:
            target (MessageTarget): Destination. ``group_id`` is treated as a
                space name (``spaces/xxx``); ``user_id`` is treated as a user
                resource name (``users/xxx``). Prefixes are added automatically
                if absent.
            content (str): Text content to send.
            reply_to (Optional[str]): Thread key for in-thread replies.
                Defaults to None.

        Returns:
            str: Google Chat message resource name (e.g.
                ``spaces/xxx/messages/yyy``), or ``"sent"`` if absent.

        Raises:
            RuntimeError: If the client is not connected or the API returns
                an error.
            ValueError: If neither ``target.user_id`` nor ``target.group_id``
                is set.
        """
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
        """Send media to a Google Chat space or user.

        Sends URL-based media as a text message with the URL appended. Local
        file uploads are not supported due to the complexity of the Google
        Chat upload protocol.

        Args:
            target (MessageTarget): Destination space or user.
            media (MediaAttachment): Media to send. Only ``url`` is supported.

        Returns:
            str: Google Chat message resource name of the sent message.

        Raises:
            NotImplementedError: If ``media.file_path`` is set (local file
                upload is not implemented).
            ValueError: If neither ``media.url`` nor ``media.file_path`` is
                provided.
        """
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
        """Add an emoji reaction to a Google Chat message.

        Parses the message resource name to extract the space, then POSTs
        a reaction. Failures are logged as warnings rather than raised.

        Args:
            message_id (str): Google Chat message resource name in the format
                ``spaces/xxx/messages/yyy``.
            emoji (str): Unicode emoji character to use as the reaction.

        Raises:
            RuntimeError: If the client is not connected.
        """
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
        """Parse and handle an incoming Google Chat webhook payload.

        Handles ``MESSAGE``, ``ADDED_TO_SPACE``, and ``REMOVED_FROM_SPACE``
        event types.

        Args:
            payload (Dict[str, Any]): Raw JSON webhook payload from Google Chat.

        Returns:
            Optional[Message]: Parsed message for ``MESSAGE``,
                ``ADDED_TO_SPACE``, or ``REMOVED_FROM_SPACE`` events, or
                ``None`` for unrecognized event types or parse errors.
        """
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
                    timestamp=now(),
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
                    timestamp=now(),
                    metadata={"space": payload.get("space", {}).get("name")},
                )
            
            elif event_type == "REMOVED_FROM_SPACE":
                return Message(
                    id=f"removed-{payload.get('timestamp', '')}",
                    channel="googlechat",
                    sender=payload.get("user", {}).get("name", ""),
                    sender_name=payload.get("user", {}).get("displayName", "Google Chat User"),
                    content="[removed_from_space]",
                    timestamp=now(),
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error handling Google Chat webhook: {e}")
            return None
    
    async def _listen(self) -> None:
        """Polling fallback for Google Chat (minimal implementation).

        Google Chat primarily uses webhooks for inbound messages. This method
        sleeps in a loop as a placeholder. Full polling would require the
        ``spaces.list`` and ``spaces.messages.list`` APIs.
        """
        # Google Chat primarily uses webhooks, but we can poll for new messages
        # This requires the spaces.list and spaces.messages.list APIs
        while self._running:
            await asyncio.sleep(30)
