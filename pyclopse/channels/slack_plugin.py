"""SlackPlugin — unified channel plugin for Slack.

Webhook-based inbound via the generic ``/webhook/slack`` route (or the legacy
``/webhook/slack`` route).  Outbound via the Slack Web API.  Supports
multi-bot (one bot per agent), threading, and allowlist/denylist.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from pyclopse.channels.base import MediaAttachment, MessageTarget
from pyclopse.channels.plugin import (
    ChannelCapabilities, ChannelConfig, ChannelPlugin, GatewayHandle,
)

_logger = logging.getLogger("pyclopse.channels.slack")


# ---------------------------------------------------------------------------
# Slack-specific config
# ---------------------------------------------------------------------------

class SlackBotConfig(BaseModel):
    """Per-bot Slack config within a multi-bot setup.

    Fields left as ``None`` inherit from the parent ``SlackChannelConfig``.
    """
    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    signing_secret: Optional[str] = Field(default=None, validation_alias="signingSecret")
    agent: Optional[str] = None
    allowed_users: Optional[list] = Field(default=None, validation_alias="allowedUsers")
    denied_users: Optional[list] = Field(default=None, validation_alias="deniedUsers")
    threading: Optional[bool] = None


class SlackChannelConfig(ChannelConfig):
    """Slack channel configuration — extends base with platform fields."""

    bot_token: Optional[str] = Field(default=None, validation_alias="botToken")
    signing_secret: Optional[str] = Field(default=None, validation_alias="signingSecret")
    threading: bool = True
    """Reply in thread when message is part of a Slack thread."""
    bots: Dict[str, SlackBotConfig] = Field(default_factory=dict)
    """Multi-bot: named bots, each routing to a specific agent."""

    def effective_config_for_bot(self, name: str) -> SlackBotConfig:
        """Return fully-resolved config for *name*, inheriting parent defaults."""
        bot = self.bots[name]
        return SlackBotConfig.model_validate({
            "botToken": bot.bot_token or self.bot_token,
            "signingSecret": bot.signing_secret or self.signing_secret,
            "agent": bot.agent,
            "allowedUsers": bot.allowed_users if bot.allowed_users is not None else self.allowed_users,
            "deniedUsers": bot.denied_users if bot.denied_users is not None else self.denied_users,
            "threading": bot.threading if bot.threading is not None else self.threading,
        })


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class SlackPlugin(ChannelPlugin):
    """Slack channel plugin — webhook-based, multi-bot, threading support."""

    name = "slack"
    config_schema = SlackChannelConfig
    capabilities = ChannelCapabilities(
        streaming=False,
        media=True,
        reactions=True,
        threads=True,
        typing_indicator=False,
        message_edit=True,
        html_formatting=False,  # Slack uses mrkdwn
        max_message_length=4000,
    )

    def __init__(self) -> None:
        self._gw: Optional[GatewayHandle] = None
        self._config: Optional[SlackChannelConfig] = None
        # bot_name → (AsyncWebClient, effective_config)
        self._bots: Dict[str, Tuple[Any, Any]] = {}
        # bot_token → bot_name (for webhook routing by token)
        self._token_to_bot: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, gateway: GatewayHandle) -> None:
        self._gw = gateway
        self._config = self._load_config(gateway)

        if not self._config.enabled:
            _logger.info("Slack disabled or not configured")
            return

        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            _logger.warning("slack-sdk not installed, Slack disabled")
            return

        bots_to_init = self._resolve_bots()
        if not bots_to_init:
            _logger.warning("Slack enabled but no bot tokens configured")
            return

        for bot_name, token, effective_cfg in bots_to_init:
            try:
                client = AsyncWebClient(token=token)
                self._bots[bot_name] = (client, effective_cfg)
                self._token_to_bot[token] = bot_name
                _logger.info(
                    f"Slack bot '{bot_name}' initialized "
                    f"(agent={getattr(effective_cfg, 'agent', None) or 'first'}, "
                    f"threading={getattr(effective_cfg, 'threading', True)})"
                )
            except Exception as e:
                _logger.error(f"Failed to initialize Slack bot '{bot_name}': {e}")

    async def stop(self) -> None:
        self._bots.clear()
        self._token_to_bot.clear()

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send_message(
        self,
        target: MessageTarget,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return None
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return None
        try:
            post_kwargs: Dict[str, Any] = {"channel": channel_id, "text": text}
            thread_ts = kwargs.get("thread_ts") or target.thread_id
            if thread_ts:
                post_kwargs["thread_ts"] = thread_ts
            resp = await client.chat_postMessage(**post_kwargs)
            return resp.get("ts")
        except Exception as e:
            _logger.error(f"send_message failed: {e}")
            return None

    async def edit_message(
        self,
        target: MessageTarget,
        message_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return
        try:
            await client.chat_update(channel=channel_id, ts=message_id, text=text)
        except Exception as e:
            _logger.error(f"edit_message failed: {e}")

    async def send_media(
        self,
        target: MessageTarget,
        media: MediaAttachment,
        **kwargs: Any,
    ) -> Optional[str]:
        client = self._resolve_client(kwargs.get("bot_name"))
        if not client:
            return None
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return None
        try:
            if media.file_path:
                resp = await client.files_upload_v2(
                    channel=channel_id,
                    file=media.file_path,
                    initial_comment=media.caption or "",
                )
                return resp.get("ts")
            elif media.url:
                blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": media.caption or media.url},
                        "accessory": {
                            "type": "image",
                            "image_url": media.url,
                            "alt_text": media.caption or "attachment",
                        },
                    }
                ] if (media.mime_type or "").startswith("image/") else [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"<{media.url}|{media.caption or 'attachment'}>"},
                    }
                ]
                resp = await client.chat_postMessage(channel=channel_id, blocks=blocks, text=media.caption or media.url)
                return resp.get("ts")
        except Exception as e:
            _logger.error(f"send_media failed: {e}")
        return None

    async def react(
        self,
        target: MessageTarget,
        message_id: str,
        emoji: str,
    ) -> None:
        client = self._resolve_client()
        if not client:
            return
        channel_id = target.user_id or target.group_id
        if not channel_id:
            return
        # Slack emoji names don't use colons
        name = emoji.strip(":")
        try:
            await client.reactions_add(channel=channel_id, timestamp=message_id, name=name)
        except Exception as e:
            _logger.debug(f"react failed: {e}")

    async def send_typing(self, target: MessageTarget) -> None:
        pass  # Slack has no typing indicator API

    # ── Webhook ───────────────────────────────────────────────────────────

    async def handle_webhook(
        self,
        request_body: bytes,
        headers: Dict[str, str],
        query_params: Dict[str, str],
    ) -> Optional[Any]:
        """Handle Slack Events API webhook."""
        try:
            payload = json.loads(request_body)
        except json.JSONDecodeError:
            _logger.warning("Slack webhook: invalid JSON")
            return None

        # URL verification challenge
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        # Verify signature if signing_secret configured
        signing_secret = self._config.signing_secret if self._config else None
        if not signing_secret:
            # Try first bot's signing secret
            for _bn, (_c, cfg) in self._bots.items():
                signing_secret = getattr(cfg, "signing_secret", None)
                if signing_secret:
                    break
        if signing_secret:
            timestamp = headers.get("x-slack-request-timestamp", "")
            signature = headers.get("x-slack-signature", "")
            if not _verify_slack_signature(request_body, timestamp, signature, signing_secret):
                _logger.warning("Slack webhook signature verification failed")
                return None

        # Process event
        event = payload.get("event", {})
        if not event:
            return None

        # Spawn as background task
        asyncio.create_task(self._process_event(event))
        return None  # 200 OK

    async def _process_event(self, event: Dict[str, Any]) -> None:
        """Process one Slack event (runs as background task)."""
        if not self._gw:
            return

        # Only handle message events, ignore bot messages
        if event.get("type") != "message":
            return
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return

        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        if not text or not user_id:
            return

        # Resolve bot (for now, use first/default — Slack events don't carry
        # which bot received the message in the same way Telegram does)
        bot_name = "_default"
        if self._bots:
            bot_name = next(iter(self._bots))

        effective_cfg = self._effective_config(bot_name)

        # Dedup
        if self._gw.is_duplicate(f"slack/{bot_name}", ts):
            return

        # Access control
        allowed = getattr(effective_cfg, "allowed_users", []) or []
        denied = getattr(effective_cfg, "denied_users", []) or []
        if not self._gw.check_access(user_id, allowed, denied):
            _logger.debug(f"Ignored Slack message from unauthorized user {user_id}")
            return

        agent_id = self._agent_id_for_bot(bot_name)
        threading_enabled = getattr(effective_cfg, "threading", True)

        _logger.info(
            f"Slack message received: bot={bot_name} user={user_id} "
            f"channel={channel_id} agent={agent_id} text={text[:60]!r}"
        )

        # Command interception
        if text.startswith("/"):
            reply = await self._gw.dispatch_command(
                channel="slack",
                user_id=user_id,
                text=text,
                thread_id=thread_ts or ts if threading_enabled else None,
                agent_id=agent_id,
            )
            if reply is not None:
                client = self._resolve_client(bot_name)
                if client:
                    post_kwargs: Dict[str, Any] = {"channel": channel_id, "text": reply}
                    if threading_enabled and (thread_ts or ts):
                        post_kwargs["thread_ts"] = thread_ts or ts
                    try:
                        await client.chat_postMessage(**post_kwargs)
                    except Exception as e:
                        _logger.error(f"Failed to send command reply: {e}")
                return

        # Session key: thread-based or user-based
        if threading_enabled:
            session_id = thread_ts or ts
        else:
            session_id = user_id

        # Register endpoint
        self._gw.register_endpoint(agent_id, "slack", {
            "sender_id": channel_id,
            "sender": user_id,
            "bot_name": bot_name,
            "thread_ts": thread_ts or ts if threading_enabled else None,
        })

        # Dispatch
        try:
            response = await self._gw.dispatch(
                channel="slack",
                user_id=user_id,
                user_name=user_id,  # Slack doesn't include display name in events
                text=text,
                message_id=ts,
                agent_id=agent_id,
            )
            if response:
                from pyclopse.agents.runner import strip_thinking_tags
                clean = strip_thinking_tags(response)
                client = self._resolve_client(bot_name)
                if client:
                    post_kwargs = {"channel": channel_id, "text": clean}
                    if threading_enabled and (thread_ts or ts):
                        post_kwargs["thread_ts"] = thread_ts or ts
                    for chunk in self._gw.split_message(clean, 4000):
                        ck = dict(post_kwargs)
                        ck["text"] = chunk
                        await client.chat_postMessage(**ck)
        except asyncio.CancelledError:
            _logger.info(f"Slack message cancelled for {user_id}")
        except Exception as e:
            _logger.error(f"Error handling Slack message from {user_id}: {e}")

    # ── Bot resolution helpers ────────────────────────────────────────────

    def _resolve_bots(self) -> List[Tuple[str, str, Any]]:
        """Build (bot_name, token, effective_config) list."""
        cfg = self._config
        if not cfg:
            return []
        result: List[Tuple[str, str, Any]] = []
        if cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.bot_token:
                    result.append((bot_name, effective.bot_token, effective))
                else:
                    _logger.warning(f"Slack bot '{bot_name}' has no botToken, skipping")
        elif cfg.bot_token:
            result.append(("_default", cfg.bot_token, cfg))
        return result

    def _resolve_client(self, bot_name: Optional[str] = None) -> Optional[Any]:
        """Get an AsyncWebClient by bot name, or first available."""
        if bot_name and bot_name in self._bots:
            return self._bots[bot_name][0]
        if self._bots:
            return next(iter(self._bots.values()))[0]
        return None

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

    def bot_for_agent(self, agent_id: str) -> Tuple[Optional[Any], Optional[str]]:
        """Return (client, bot_name) for the bot configured for *agent_id*."""
        cfg = self._config
        if cfg and cfg.bots:
            for bot_name, _bot_cfg in cfg.bots.items():
                effective = cfg.effective_config_for_bot(bot_name)
                if effective.agent == agent_id and bot_name in self._bots:
                    return self._bots[bot_name][0], bot_name
        if self._bots:
            bot_name = next(iter(self._bots))
            return self._bots[bot_name][0], bot_name
        return None, None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Verify Slack request signature (HMAC-SHA256)."""
    if not signature or not timestamp:
        return False
    # Reject requests older than 5 minutes
    try:
        if abs(_time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    sig_basestring = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(
        secret.encode(), sig_basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
