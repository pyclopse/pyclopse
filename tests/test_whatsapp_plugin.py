"""
Tests for WhatsAppPlugin — Meta Cloud API channel plugin.

Covers:
  - Webhook verification (GET challenge)
  - Webhook signature verification (HMAC-SHA256)
  - Inbound message parsing and dispatch
  - Access control
  - Dedup
  - Command dispatch
  - Outbound (send_message, send_media, react)
  - Phone normalization
  - Config schema
  - Stop lifecycle
"""

import hashlib
import hmac
import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from pyclopse.channels.whatsapp_plugin import (
    WhatsAppPlugin, WhatsAppChannelConfig, _normalize_phone, _verify_signature,
)
from pyclopse.channels.base import MessageTarget, MediaAttachment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gw_handle(
    dispatch_return="agent reply",
    command_return=None,
    is_duplicate=False,
    check_access=True,
    agent_id="test_agent",
):
    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_return)
    handle.dispatch_command = AsyncMock(return_value=command_return)
    handle.is_duplicate = MagicMock(return_value=is_duplicate)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value=agent_id)
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    config = MagicMock()
    config.channels = MagicMock()
    type(handle).config = PropertyMock(return_value=config)
    return handle


def _make_plugin(
    check_access=True,
    is_duplicate=False,
    dispatch_return="agent reply",
    command_return=None,
    webhook_verify_token="test-verify",
    app_secret="test-secret",
):
    plugin = WhatsAppPlugin()
    plugin._config = WhatsAppChannelConfig(
        enabled=True,
        phoneId="123456",
        accessToken="test-token",
        webhookVerifyToken=webhook_verify_token,
        appSecret=app_secret,
    )
    # Mock httpx client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"messages": [{"id": "wamid.123"}]})
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_resp)
    api_base = "https://graph.facebook.com/v21.0/123456"
    plugin._bots = {"_default": (mock_http, api_base, plugin._config)}
    plugin._phone_to_bot = {"123456": "_default"}

    handle = _make_gw_handle(
        dispatch_return=dispatch_return,
        command_return=command_return,
        is_duplicate=is_duplicate,
        check_access=check_access,
    )
    plugin._gw = handle
    return plugin, handle


def _make_webhook_payload(
    sender="15551234567",
    text="hello",
    msg_id="wamid.abc123",
    sender_name="Alice",
):
    return json.dumps({
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123456"},
                    "contacts": [{"wa_id": sender, "profile": {"name": sender_name}}],
                    "messages": [{
                        "from": sender,
                        "id": msg_id,
                        "type": "text",
                        "text": {"body": text},
                    }],
                }
            }]
        }]
    }).encode()


def _sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Webhook verification (GET challenge)
# ---------------------------------------------------------------------------

class TestWebhookVerification:

    async def test_valid_challenge_returns_value(self):
        plugin, _ = _make_plugin()
        result = await plugin.handle_webhook(
            b"",
            {},
            {"hub.mode": "subscribe", "hub.verify_token": "test-verify", "hub.challenge": "challenge123"},
        )
        assert result == "challenge123"

    async def test_wrong_token_raises(self):
        plugin, _ = _make_plugin()
        with pytest.raises(Exception, match="Verification failed"):
            await plugin.handle_webhook(
                b"",
                {},
                {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
            )


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

class TestSignatureVerification:

    def test_valid_signature(self):
        body = b'{"test": true}'
        secret = "mysecret"
        sig = _sign_payload(body, secret)
        assert _verify_signature(body, sig, secret) is True

    def test_invalid_signature(self):
        assert _verify_signature(b"body", "sha256=invalid", "secret") is False

    def test_missing_signature(self):
        assert _verify_signature(b"body", "", "secret") is False


# ---------------------------------------------------------------------------
# Inbound message parsing
# ---------------------------------------------------------------------------

class TestWebhookInbound:

    async def test_text_message_dispatched(self):
        plugin, handle = _make_plugin()
        body = _make_webhook_payload(text="hello world")
        sig = _sign_payload(body, "test-secret")
        await plugin.handle_webhook(body, {"x-hub-signature-256": sig}, {})
        # Processing is async — give it a tick
        await asyncio.sleep(0.05)
        handle.dispatch.assert_called_once()
        call_kwargs = handle.dispatch.call_args.kwargs
        assert call_kwargs["text"] == "hello world"
        assert call_kwargs["channel"] == "whatsapp"

    async def test_non_text_message_ignored(self):
        plugin, handle = _make_plugin()
        payload = json.dumps({
            "entry": [{"changes": [{"value": {
                "metadata": {},
                "contacts": [],
                "messages": [{"from": "123", "id": "x", "type": "image"}],
            }}]}]
        }).encode()
        sig = _sign_payload(payload, "test-secret")
        await plugin.handle_webhook(payload, {"x-hub-signature-256": sig}, {})
        await asyncio.sleep(0.05)
        handle.dispatch.assert_not_called()

    async def test_empty_payload_returns_none(self):
        plugin, _ = _make_plugin()
        result = await plugin.handle_webhook(b"", {}, {})
        assert result is None

    async def test_invalid_json_returns_none(self):
        plugin, handle = _make_plugin(app_secret=None)
        plugin._config.app_secret = None
        result = await plugin.handle_webhook(b"not json", {}, {})
        assert result is None


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    async def test_allowed_user_passes(self):
        plugin, handle = _make_plugin(check_access=True)
        body = _make_webhook_payload()
        sig = _sign_payload(body, "test-secret")
        await plugin.handle_webhook(body, {"x-hub-signature-256": sig}, {})
        await asyncio.sleep(0.05)
        handle.dispatch.assert_called_once()

    async def test_denied_user_blocked(self):
        plugin, handle = _make_plugin(check_access=False)
        body = _make_webhook_payload()
        sig = _sign_payload(body, "test-secret")
        await plugin.handle_webhook(body, {"x-hub-signature-256": sig}, {})
        await asyncio.sleep(0.05)
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDedup:

    async def test_duplicate_dropped(self):
        plugin, handle = _make_plugin(is_duplicate=True)
        body = _make_webhook_payload()
        sig = _sign_payload(body, "test-secret")
        await plugin.handle_webhook(body, {"x-hub-signature-256": sig}, {})
        await asyncio.sleep(0.05)
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestCommandDispatch:

    async def test_slash_command_intercepted(self):
        plugin, handle = _make_plugin(command_return="Done!")
        body = _make_webhook_payload(text="/help")
        sig = _sign_payload(body, "test-secret")
        await plugin.handle_webhook(body, {"x-hub-signature-256": sig}, {})
        await asyncio.sleep(0.05)
        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

class TestOutbound:

    async def test_send_message_payload(self):
        plugin, _ = _make_plugin()
        mock_http = plugin._bots["_default"][0]
        target = MessageTarget(channel="whatsapp", user_id="+15551234567")
        result = await plugin.send_message(target, "Hello!")
        assert result == "wamid.123"
        call_kwargs = mock_http.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["type"] == "text"
        assert payload["text"]["body"] == "Hello!"
        assert payload["to"] == "15551234567"  # + stripped

    async def test_send_media_image(self):
        plugin, _ = _make_plugin()
        mock_http = plugin._bots["_default"][0]
        target = MessageTarget(channel="whatsapp", user_id="15551234567")
        media = MediaAttachment(url="https://example.com/img.jpg", mime_type="image/jpeg")
        result = await plugin.send_media(target, media)
        assert result == "wamid.123"
        payload = mock_http.post.call_args.kwargs.get("json") or mock_http.post.call_args[1].get("json")
        assert payload["type"] == "image"
        assert payload["image"]["link"] == "https://example.com/img.jpg"

    async def test_send_media_document(self):
        plugin, _ = _make_plugin()
        mock_http = plugin._bots["_default"][0]
        target = MessageTarget(channel="whatsapp", user_id="15551234567")
        media = MediaAttachment(url="https://example.com/file.pdf", mime_type="application/pdf")
        await plugin.send_media(target, media)
        payload = mock_http.post.call_args.kwargs.get("json") or mock_http.post.call_args[1].get("json")
        assert payload["type"] == "document"

    async def test_react(self):
        plugin, _ = _make_plugin()
        mock_http = plugin._bots["_default"][0]
        target = MessageTarget(channel="whatsapp", user_id="15551234567")
        await plugin.react(target, "wamid.abc", "👍")
        payload = mock_http.post.call_args.kwargs.get("json") or mock_http.post.call_args[1].get("json")
        assert payload["type"] == "reaction"
        assert payload["reaction"]["emoji"] == "👍"
        assert payload["reaction"]["message_id"] == "wamid.abc"


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

class TestPhoneNormalization:

    def test_strips_plus(self):
        assert _normalize_phone("+15551234567") == "15551234567"

    def test_strips_dashes_and_spaces(self):
        assert _normalize_phone("+1-555-123-4567") == "15551234567"

    def test_bare_digits_unchanged(self):
        assert _normalize_phone("15551234567") == "15551234567"


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

class TestConfigSchema:

    def test_default_config(self):
        cfg = WhatsAppChannelConfig()
        assert cfg.enabled is True
        assert cfg.phone_id is None
        assert cfg.api_version == "v21.0"

    def test_from_yaml_dict(self):
        cfg = WhatsAppChannelConfig.model_validate({
            "enabled": True,
            "phoneId": "12345",
            "accessToken": "token",
            "webhookVerifyToken": "verify",
            "appSecret": "secret",
            "allowedUsers": ["+15551234567"],
        })
        assert cfg.phone_id == "12345"
        assert cfg.access_token == "token"
        assert cfg.webhook_verify_token == "verify"
        assert cfg.app_secret == "secret"

    def test_plugin_declares_schema(self):
        assert WhatsAppPlugin.config_schema is WhatsAppChannelConfig


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class TestStop:

    async def test_stop_closes_httpx(self):
        plugin = WhatsAppPlugin()
        mock_http = AsyncMock()
        plugin._bots = {"_default": (mock_http, "https://example.com", None)}
        plugin._phone_to_bot = {"123456": "_default"}
        await plugin.stop()
        mock_http.aclose.assert_called_once()
        assert plugin._bots == {}
        assert plugin._phone_to_bot == {}

    async def test_stop_with_no_client(self):
        plugin = WhatsAppPlugin()
        await plugin.stop()  # Should not raise
