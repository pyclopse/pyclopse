"""
Tests for Telegram typing indicator in TelegramPlugin._handle_message.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(user_id=111, message_id=1, text="hello"):
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Tester"
    msg.chat.id = 42
    msg.message_id = message_id
    msg.text = text
    msg.message_thread_id = None
    return msg


def _make_plugin(typing_indicator=True, dispatch_response="ok"):
    """Build a TelegramPlugin stub with configurable typing indicator setting."""
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
        "allowedUsers": [111],
        "typingIndicator": typing_indicator,
    })

    config = Config(
        channels=ChannelsConfig(telegram=telegram_cfg),
        agents=AgentsConfig(),
        security=SecurityConfig(),
    )

    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_response)
    handle.dispatch_command = AsyncMock(return_value=None)
    handle.is_duplicate = MagicMock(return_value=False)
    handle.check_access = MagicMock(return_value=True)
    handle.resolve_agent_id = MagicMock(return_value="test_agent")
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    type(handle).config = PropertyMock(return_value=config)

    plugin._gw = handle
    plugin._telegram_config = telegram_cfg

    return plugin, bot, handle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTypingIndicator:

    @pytest.mark.asyncio
    async def test_send_chat_action_called_when_enabled(self):
        plugin, bot, _ = _make_plugin(typing_indicator=True)
        await plugin._handle_message(_make_message(), "_default", bot)
        bot.send_chat_action.assert_called()
        args = bot.send_chat_action.call_args
        assert args.kwargs.get("action") == "typing" or args.args[-1] == "typing"

    @pytest.mark.asyncio
    async def test_send_chat_action_not_called_when_disabled(self):
        plugin, bot, _ = _make_plugin(typing_indicator=False)
        await plugin._handle_message(_make_message(), "_default", bot)
        bot.send_chat_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_typing_uses_correct_chat_id(self):
        plugin, bot, _ = _make_plugin(typing_indicator=True)
        msg = _make_message()
        msg.chat.id = 12345
        await plugin._handle_message(msg, "_default", bot)
        call_kwargs = bot.send_chat_action.call_args.kwargs
        assert call_kwargs.get("chat_id") == "12345" or call_kwargs.get("chat_id") == 12345

    @pytest.mark.asyncio
    async def test_response_sent_even_with_typing(self):
        plugin, bot, _ = _make_plugin(typing_indicator=True, dispatch_response="my response")
        await plugin._handle_message(_make_message(), "_default", bot)
        bot.send_message.assert_called()
        # Find the call that has our response text (may be among multiple calls)
        found = False
        for c in bot.send_message.call_args_list:
            text = c.kwargs.get("text", "")
            if "my response" in text:
                found = True
                break
        assert found, f"Expected 'my response' in send_message calls: {bot.send_message.call_args_list}"

    @pytest.mark.asyncio
    async def test_typing_cancelled_after_response(self):
        """After dispatch returns, the typing loop must be cancelled."""
        plugin, bot, _ = _make_plugin(typing_indicator=True)
        # We verify the method completes without hanging (typing task is cleaned up).
        await asyncio.wait_for(
            plugin._handle_message(_make_message(), "_default", bot),
            timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_typing_cancelled_even_on_error(self):
        """Typing task is cancelled even when dispatch raises."""
        plugin, bot, handle = _make_plugin(typing_indicator=True)
        handle.dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        # Should not hang -- typing task cleaned up in finally block
        await asyncio.wait_for(
            plugin._handle_message(_make_message(), "_default", bot),
            timeout=2.0,
        )
