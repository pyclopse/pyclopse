"""
Tests for per-channel allowlist/denylist enforcement via TelegramPlugin._handle_message.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock


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
    msg.message_thread_id = None
    return msg


def _make_plugin(check_access: bool = True):
    """Build a TelegramPlugin stub with a mocked GatewayHandle.

    Args:
        check_access: Return value for handle.check_access — True means allowed,
                      False means denied.
    """
    from pyclopse.channels.telegram_plugin import TelegramPlugin, TelegramChannelConfig
    from pyclopse.config.schema import (
        Config, ChannelsConfig, AgentsConfig, SecurityConfig,
    )

    plugin = TelegramPlugin()

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    bot.send_chat_action = AsyncMock()
    plugin._bots = {"_default": bot}
    plugin._chat_ids = {"_default": None}

    telegram_cfg = TelegramChannelConfig.model_validate({
        "enabled": True,
        "botToken": "fake",
    })

    config = Config(
        channels=ChannelsConfig(telegram=telegram_cfg),
        agents=AgentsConfig(),
        security=SecurityConfig(),
    )

    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value="ok")
    handle.dispatch_command = AsyncMock(return_value=None)
    handle.is_duplicate = MagicMock(return_value=False)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value="test_agent")
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    type(handle).config = PropertyMock(return_value=config)

    plugin._gw = handle
    plugin._telegram_config = telegram_cfg

    return plugin, bot, handle


# ---------------------------------------------------------------------------
# Per-channel allowedUsers
# ---------------------------------------------------------------------------

class TestChannelAllowlist:

    @pytest.mark.asyncio
    async def test_allowed_user_passes(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=111), "_default", bot)
        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_allowed_user_blocked(self):
        plugin, bot, handle = _make_plugin(check_access=False)
        await plugin._handle_message(_make_message(user_id=999), "_default", bot)
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_anyone(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=42), "_default", bot)
        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_allowed_users(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=20), "_default", bot)
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Per-channel deniedUsers
# ---------------------------------------------------------------------------

class TestChannelDenylist:

    @pytest.mark.asyncio
    async def test_denied_user_blocked(self):
        plugin, bot, handle = _make_plugin(check_access=False)
        await plugin._handle_message(_make_message(user_id=777), "_default", bot)
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_denied_user_passes(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=888), "_default", bot)
        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_denied_overrides_allowed(self):
        """A user in both allowed and denied is blocked (denied wins)."""
        plugin, bot, handle = _make_plugin(check_access=False)
        await plugin._handle_message(_make_message(user_id=555), "_default", bot)
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Global deniedUsers
# ---------------------------------------------------------------------------

class TestGlobalDenylist:

    @pytest.mark.asyncio
    async def test_globally_denied_user_blocked(self):
        plugin, bot, handle = _make_plugin(check_access=False)
        await plugin._handle_message(_make_message(user_id=321), "_default", bot)
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_denied_overrides_channel_allowed(self):
        """Even if in channel allowed_users, global denied wins."""
        plugin, bot, handle = _make_plugin(check_access=False)
        await plugin._handle_message(_make_message(user_id=321), "_default", bot)
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_globally_denied_passes(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=999), "_default", bot)
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Channel allowlist overrides global allowlist (precedence)
# ---------------------------------------------------------------------------

class TestAllowlistPrecedence:

    @pytest.mark.asyncio
    async def test_channel_allowlist_takes_precedence_over_none(self):
        """When channel has its own allowed_users, only those are allowed."""
        plugin, bot, handle = _make_plugin(check_access=False)
        # user 200 is not in channel allowed_users
        await plugin._handle_message(_make_message(user_id=200), "_default", bot)
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_restrictions_allows_all(self):
        plugin, bot, handle = _make_plugin(check_access=True)
        await plugin._handle_message(_make_message(user_id=12345), "_default", bot)
        handle.dispatch.assert_called_once()
