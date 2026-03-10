"""
Tests for per-channel allowlist/denylist enforcement in _handle_telegram_message.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(user_id: int, message_id: int = 1, text: str = "hello"):
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = f"User{user_id}"
    msg.chat.id = 99
    msg.message_id = message_id
    msg.text = text
    return msg


def _make_gateway(
    allowed_users=None,
    denied_users=None,
    global_denied=None,
):
    """Gateway stub with configurable allowlist/denylist."""
    from pyclaw.core.gateway import Gateway
    from pyclaw.config.schema import (
        Config, ChannelsConfig, TelegramConfig, AgentsConfig, SecurityConfig,
    )

    gw = Gateway.__new__(Gateway)
    gw._is_running = True
    gw._initialized = True
    gw._logger = MagicMock()
    gw._audit_logger = None
    gw._telegram_bot = AsyncMock()
    gw._telegram_chat_id = None
    gw._telegram_polling_task = None
    gw._session_manager = MagicMock()
    gw._agent_manager = MagicMock()
    gw._agent_manager.agents = {}
    gw._channels = {}
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    gw._command_registry = MagicMock()
    gw._command_registry.dispatch = AsyncMock(return_value=None)

    telegram_cfg = TelegramConfig.model_validate({
        "enabled": True,
        "botToken": "fake",
        "allowedUsers": allowed_users or [],
        "deniedUsers": denied_users or [],
    })
    security_cfg = SecurityConfig.model_validate({
        "deniedUsers": global_denied or [],
    })
    channels_cfg = ChannelsConfig(telegram=telegram_cfg)
    gw._config = Config(channels=channels_cfg, agents=AgentsConfig(), security=security_cfg)

    # Mock handle_message to return a response
    gw.handle_message = AsyncMock(return_value="ok")
    mock_sm = MagicMock()
    mock_sm.get_or_create_session = AsyncMock(return_value=MagicMock())
    gw._session_manager = mock_sm

    return gw


# ---------------------------------------------------------------------------
# Per-channel allowedUsers
# ---------------------------------------------------------------------------

class TestChannelAllowlist:

    @pytest.mark.asyncio
    async def test_allowed_user_passes(self):
        gw = _make_gateway(allowed_users=[111])
        await gw._handle_telegram_message(_make_message(user_id=111))
        gw.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_allowed_user_blocked(self):
        gw = _make_gateway(allowed_users=[111])
        await gw._handle_telegram_message(_make_message(user_id=999))
        gw.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_anyone(self):
        gw = _make_gateway(allowed_users=[])
        await gw._handle_telegram_message(_make_message(user_id=42))
        gw.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_allowed_users(self):
        gw = _make_gateway(allowed_users=[10, 20, 30])
        await gw._handle_telegram_message(_make_message(user_id=20))
        gw.handle_message.assert_called_once()


# ---------------------------------------------------------------------------
# Per-channel deniedUsers
# ---------------------------------------------------------------------------

class TestChannelDenylist:

    @pytest.mark.asyncio
    async def test_denied_user_blocked(self):
        gw = _make_gateway(denied_users=[777])
        await gw._handle_telegram_message(_make_message(user_id=777))
        gw.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_denied_user_passes(self):
        gw = _make_gateway(denied_users=[777])
        await gw._handle_telegram_message(_make_message(user_id=888))
        gw.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_denied_overrides_allowed(self):
        """A user in both allowed and denied is blocked (denied wins)."""
        gw = _make_gateway(allowed_users=[555], denied_users=[555])
        await gw._handle_telegram_message(_make_message(user_id=555))
        gw.handle_message.assert_not_called()


# ---------------------------------------------------------------------------
# Global deniedUsers
# ---------------------------------------------------------------------------

class TestGlobalDenylist:

    @pytest.mark.asyncio
    async def test_globally_denied_user_blocked(self):
        gw = _make_gateway(global_denied=[321])
        await gw._handle_telegram_message(_make_message(user_id=321))
        gw.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_denied_overrides_channel_allowed(self):
        """Even if in channel allowed_users, global denied wins."""
        gw = _make_gateway(allowed_users=[321], global_denied=[321])
        await gw._handle_telegram_message(_make_message(user_id=321))
        gw.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_globally_denied_passes(self):
        gw = _make_gateway(global_denied=[321])
        await gw._handle_telegram_message(_make_message(user_id=999))
        gw.handle_message.assert_called_once()


# ---------------------------------------------------------------------------
# Channel allowlist overrides global allowlist (precedence)
# ---------------------------------------------------------------------------

class TestAllowlistPrecedence:

    @pytest.mark.asyncio
    async def test_channel_allowlist_takes_precedence_over_none(self):
        """When channel has its own allowed_users, only those are allowed."""
        gw = _make_gateway(allowed_users=[100])
        # user 200 is not in channel allowed_users
        await gw._handle_telegram_message(_make_message(user_id=200))
        gw.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_restrictions_allows_all(self):
        gw = _make_gateway()  # no allowed or denied lists
        await gw._handle_telegram_message(_make_message(user_id=12345))
        gw.handle_message.assert_called_once()
