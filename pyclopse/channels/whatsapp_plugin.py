"""WhatsAppPlugin — unified channel plugin for WhatsApp via Meta Cloud API.

Supports multi-bot (one phone number per agent).  Webhook-based inbound via
the generic ``/webhook/whatsapp`` route.  Outbound via the WhatsApp Cloud API.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from pyclopse.channels.base import MediaAttachment, MessageTarget
from pyclopse.channels.plugin import (
    ChannelCapabilities, ChannelConfig, ChannelPlugin, GatewayHandle,
)

_logger = logging.getLogger("pyclopse.channels.whatsapp")

_E164_STRIP = re.compile(r"[^\d+]")


# ---------------------------------------------------------------------------
# WhatsApp-specific config
# ---------------------------------------------------------------------------

class WhatsAppBotConfig(BaseModel):
    """Per-bot (per-phone-number) WhatsApp config within a multi-bot setup.

    Fields left as ``None`` inherit from the parent ``WhatsAppChannelConfig``.
    """
    phone_id: Optional[str] = Field(default=None, validation_alias="phoneId")
    access_token: Optional[str] = Field(default=None, validation_alias="accessToken")
    agent: Optional[str] = None
    allowed_users: Optional[list] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[list] = Field(default=None, validation_alias="deniedUsers")
    webhook_verify_token: Optional[str] = Field(default=None, validation_alias="webhookVerifyToken")
    app_secret: Optional[str] = Field(default=None, validation_alias="appSecret")


class WhatsAppChannelConfig(ChannelConfig):
    """WhatsApp Cloud API configuration."""

    phone_id: Optional[str] = Field(default=None, validation_alias="phoneId")
    access_token: Optional[str] = Field(default=None, validation_alias="accessToken")
    webhook_verify_token: Optional[str] = Field(
        default=None, validation_alias="webhookVerifyToken",
    )
    app_secret: Optional[str] = Field(default=None, validation_alias="appSecret")
    api_version: str = Field(default="v21.0", validation_alias="apiVersion")
    bots: Dict[str, WhatsAppBotConfig] = Field(default_factory=dict)
    """Multi-bot: named phone numbers, each routing to a specific agent."""

    def effective_config_for_bot(self, name: str) -> WhatsAppBotConfig:
        """Return fully-resolved config for *name*, inheriting parent defaults."""
        bot = self.bots[name]
        return WhatsAppBotConfig.model_validate({
            "phoneId": bot.phone_id or self.phone_id,
            "accessToken": bot.access_token or self.access_token,
            "agent": bot.agent,
            "allowedUsers": bot.allowed_users if bot.allowed_users is not None else self.allowed_users,
            "deniedUsers": bot.denied_users if bot.denied_users is not None else self.denied_users,
            "webhookVerifyToken": bot.webhook_verify_token or self.webhook_verify_token,
            "appSecret": bot.app_secret or self.app_secret,
        })


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class WhatsAppPlugin(ChannelPlugin):
    """WhatsApp channel plugin — Meta Cloud API, webhook-based, multi-bot."""

    name = "whatsapp"
    config_schema = WhatsAppChannelConfig
    capabilities = ChannelCapabilities(
        streaming=False,
        media=True,
        reactions=True,
        threads=False,
        typing_indicator=False,
        message_edit=False,
        html_formatting=False,
        max_message_length=4096,
    )

    def __init__(self) -> None:
        self._gw: Optional[GatewayHandle] = None
        self._config: Optional[WhatsAppChannelConfig] = None
        # bot_name → (httpx.AsyncClient, api_base_url, effective_config)
        self._bots: Dict[str, Tuple[Any, str, Any]] = {}
        # phone_id → bot_name (for webhook routing by phone_id)
        self._phone_to_bot: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, gateway: GatewayHandle) -> None:
        self._gw = gateway
        self._config = self._load_config(gateway)

        if not self._config.enabled:
            _logger.info("WhatsApp disabled or not configured")
            return

        import httpx

        bots_to_init = self._resolve_bots()
        if not bots_to_init:
            _logger.warning("WhatsApp enabled but no phone_id/access_token configured")
            return

        for bot_name, phone_id, access_token, effective_cfg in bots_to_init:
            api_base = (
                f"https://graph.facebook.com/{self._config.api_version}"
                f"/{phone_id}"
            )
            client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            self._bots[bot_name] = (client, api_base, effective_cfg)
            self._phone_to_bot[phone_id] = bot_name
            _logger.info(
                f"WhatsApp bot '{bot_name}' initialized "
                f"(phone_id={phone_id}, agent={getattr(effective_cfg, 'agent', None) or 'first'})"
            )

    async def stop(self) -> None:
        for bot_name, (client, _, _) in list(self._bots.items()):
            await client.aclose()
        self._bots.clear()
        self._phone_to_bot.clear()

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send_message(
        self,
        target: MessageTarget,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        client, api_base = self._resolve_http(kwargs.get("bot_name"))
        if not client:
            return None
        phone = target.user_id
        if not phone:
            return None
        payload = {
            "messaging_product": "whatsapp",
            "to": _normalize_phone(phone),
            "type": "text",
            "text": {"body": text},
        }
        try:
            resp = await client.post(f"{api_base}/messages", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("messages", [{}])[0].get("id")
        except Exception as e:
            _logger.error(f"send_message failed: {e}")
            return None

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
        **kwargs: Any,
    ) -> Optional[str]:
        client, api_base = self._resolve_http(kwargs.get("bot_name"))
        if not client:
            return None
        phone = target.user_id
        if not phone or not media.url:
            return None
        mime = (media.mime_type or "").lower()
        if mime.startswith("image/"):
            media_type = "image"
        elif mime.startswith("video/"):
            media_type = "video"
        elif mime.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "document"
        media_obj: Dict[str, Any] = {"link": media.url}
        if media.caption:
            media_obj["caption"] = media.caption
        payload = {
            "messaging_product": "whatsapp",
            "to": _normalize_phone(phone),
            "type": media_type,
            media_type: media_obj,
        }
        try:
            resp = await client.post(f"{api_base}/messages", json=payload)
            resp.raise_for_status()
            return resp.json().get("messages", [{}])[0].get("id")
        except Exception as e:
            _logger.error(f"send_media failed: {e}")
            return None

    async def react(
        self,
        target: MessageTarget,
        message_id: str,
        emoji: str,
    ) -> None:
        client, api_base = self._resolve_http()
        if not client:
            return
        phone = target.user_id
        if not phone:
            return
        payload = {
            "messaging_product": "whatsapp",
            "to": _normalize_phone(phone),
            "type": "reaction",
            "reaction": {"message_id": message_id, "emoji": emoji},
        }
        try:
            resp = await client.post(f"{api_base}/messages", json=payload)
            resp.raise_for_status()
        except Exception as e:
            _logger.debug(f"react failed: {e}")

    async def send_typing(self, target: MessageTarget) -> None:
        pass  # WhatsApp Cloud API has no typing indicator

    # ── Webhook ───────────────────────────────────────────────────────────

    async def handle_webhook(
        self,
        request_body: bytes,
        headers: Dict[str, str],
        query_params: Dict[str, str],
    ) -> Optional[Any]:
        # GET verification challenge
        if query_params.get("hub.mode") == "subscribe":
            return self._handle_verify_challenge(query_params)
        # POST inbound message
        if request_body:
            return await self._handle_inbound_post(request_body, headers)
        return None

    def _handle_verify_challenge(self, params: Dict[str, str]) -> Any:
        token = params.get("hub.verify_token", "")
        # Check all configured verify tokens
        valid = False
        if self._config and self._config.webhook_verify_token and token == self._config.webhook_verify_token:
            valid = True
        if not valid:
            for _bn, (_c, _a, cfg) in self._bots.items():
                vt = getattr(cfg, "webhook_verify_token", None)
                if vt and token == vt:
                    valid = True
                    break
        if not valid:
            _logger.warning("WhatsApp webhook verification failed: token mismatch")
            raise Exception("Verification failed")
        challenge = params.get("hub.challenge", "")
        _logger.info("WhatsApp webhook verified")
        return challenge

    async def _handle_inbound_post(self, body: bytes, headers: Dict[str, str]) -> None:
        # Verify signature using first available app_secret
        app_secret = self._config.app_secret if self._config else None
        if not app_secret:
            for _bn, (_c, _a, cfg) in self._bots.items():
                app_secret = getattr(cfg, "app_secret", None)
                if app_secret:
                    break
        if app_secret:
            signature = headers.get("x-hub-signature-256", "")
            if not _verify_signature(body, signature, app_secret):
                _logger.warning("WhatsApp webhook signature verification failed")
                return None

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            _logger.warning("WhatsApp webhook: invalid JSON")
            return None

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                contacts = {
                    c.get("wa_id", ""): c.get("profile", {}).get("name", "")
                    for c in value.get("contacts", [])
                }
                for msg in value.get("messages", []):
                    asyncio.create_task(
                        self._process_message(msg, metadata, contacts)
                    )
        return None

    async def _process_message(
        self,
        msg: Dict[str, Any],
        metadata: Dict[str, Any],
        contacts: Dict[str, str],
    ) -> None:
        if not self._gw:
            return

        msg_type = msg.get("type", "")
        msg_id = msg.get("id", "")
        sender_phone = msg.get("from", "")

        if msg_type != "text":
            _logger.debug(f"WhatsApp: ignoring message type '{msg_type}'")
            return

        text = msg.get("text", {}).get("body", "")
        if not text:
            return

        # Route to correct bot by phone_id from metadata
        phone_id = metadata.get("phone_number_id", "")
        bot_name = self._phone_to_bot.get(phone_id, "_default")

        if self._gw.is_duplicate(f"whatsapp/{bot_name}", msg_id):
            return

        # Access control (per-bot)
        effective_cfg = self._effective_config(bot_name)
        allowed = getattr(effective_cfg, "allowed_users", []) or []
        denied = getattr(effective_cfg, "denied_users", []) or []
        if not self._gw.check_access(sender_phone, allowed, denied):
            _logger.debug(f"Ignored WhatsApp message from unauthorized {sender_phone} (bot={bot_name})")
            return

        sender_name = contacts.get(sender_phone, sender_phone)
        agent_id = self._agent_id_for_bot(bot_name)

        _logger.info(
            f"WhatsApp message received: bot={bot_name} from={sender_phone} "
            f"agent={agent_id} text={text[:60]!r}"
        )

        # Command intercept
        if text.strip().startswith("/"):
            reply = await self._gw.dispatch_command(
                channel="whatsapp",
                user_id=sender_phone,
                text=text.strip(),
                agent_id=agent_id,
            )
            if reply is not None:
                target = MessageTarget(channel="whatsapp", user_id=sender_phone)
                for chunk in self._gw.split_message(reply, 4096):
                    await self.send_message(target, chunk, bot_name=bot_name)
                return

        # Register endpoint (with bot_name for fan-out)
        self._gw.register_endpoint(agent_id, "whatsapp", {
            "sender_id": sender_phone,
            "sender": sender_name,
            "bot_name": bot_name,
        })

        try:
            response = await self._gw.dispatch(
                channel="whatsapp",
                user_id=sender_phone,
                user_name=sender_name,
                text=text,
                message_id=msg_id,
                agent_id=agent_id,
            )
            if response:
                from pyclopse.agents.runner import strip_thinking_tags
                clean = strip_thinking_tags(response)
                target = MessageTarget(channel="whatsapp", user_id=sender_phone)
                for chunk in self._gw.split_message(clean, 4096):
                    await self.send_message(target, chunk, bot_name=bot_name)
        except Exception as e:
            _logger.error(f"Error handling WhatsApp message from {sender_phone}: {e}")

    # ── Bot resolution helpers ────────────────────────────────────────────

    def _resolve_bots(self) -> List[Tuple[str, str, str, Any]]:
        """Build (bot_name, phone_id, access_token, effective_config) list."""
        cfg = self._config
        if not cfg:
            return []
        result: List[Tuple[str, str, str, Any]] = []
        if cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                pid = effective.phone_id
                tok = effective.access_token
                if pid and tok:
                    result.append((bot_name, pid, tok, effective))
                else:
                    _logger.warning(f"WhatsApp bot '{bot_name}' missing phoneId or accessToken, skipping")
        elif cfg.phone_id and cfg.access_token:
            result.append(("_default", cfg.phone_id, cfg.access_token, cfg))
        return result

    def _resolve_http(self, bot_name: Optional[str] = None) -> Tuple[Optional[Any], str]:
        """Get (httpx.AsyncClient, api_base) by bot name, or first available."""
        if bot_name and bot_name in self._bots:
            client, api_base, _ = self._bots[bot_name]
            return client, api_base
        if self._bots:
            client, api_base, _ = next(iter(self._bots.values()))
            return client, api_base
        return None, ""

    def _effective_config(self, bot_name: str) -> Any:
        cfg = self._config
        if not cfg:
            return None
        if cfg.bots and bot_name in cfg.bots:
            return cfg.effective_config_for_bot(bot_name)
        return cfg

    def _agent_id_for_bot(self, bot_name: str) -> str:
        cfg = self._config
        if cfg and cfg.bots and bot_name in cfg.bots:
            effective = cfg.effective_config_for_bot(bot_name)
            if effective.agent:
                return self._gw.resolve_agent_id(effective.agent)
        return self._gw.resolve_agent_id()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _normalize_phone(phone: str) -> str:
    """Normalize a phone number for the Cloud API (digits only, no +)."""
    return _E164_STRIP.sub("", phone).lstrip("+")


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature from Meta webhook."""
    if not signature:
        return False
    expected_sig = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature)
