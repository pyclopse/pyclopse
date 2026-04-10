"""
Tests for TelegramPlugin — the unified Telegram channel plugin.

Covers:
  - Access control (allowed/denied users, global deny, per-bot overrides)
  - Dedup (per-bot keying)
  - Agent routing (multi-bot to different agents)
  - Non-streaming response flow
  - Streaming on_chunk callback
  - Typing indicator lifecycle
  - Command dispatch
  - Bot resolution
  - send_message / edit_message / send_typing
  - Stop cancels polling tasks
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from pyclopse.channels.telegram_plugin import TelegramPlugin, _live_display
from pyclopse.channels.base import MessageTarget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gw_handle(
    dispatch_return="agent reply",
    command_return=None,
    is_duplicate=False,
    check_access=True,
    agent_id="test_agent",
    config=None,
):
    """Create a mock GatewayHandle with configurable behavior."""
    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_return)
    handle.dispatch_command = AsyncMock(return_value=command_return)
    handle.is_duplicate = MagicMock(return_value=is_duplicate)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value=agent_id)
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    if config is None:
        config = _make_config()
    type(handle).config = PropertyMock(return_value=config)
    return handle


def _make_config(
    enabled=True,
    bot_token="test-token",
    allowed_users=None,
    denied_users=None,
    bots=None,
    streaming=False,
    typing_indicator=True,
):
    """Create a mock gateway config with Telegram section."""
    tg_config = MagicMock()
    tg_config.enabled = enabled
    tg_config.bot_token = bot_token
    tg_config.allowed_users = allowed_users or []
    tg_config.denied_users = denied_users or []
    tg_config.bots = bots or {}
    tg_config.streaming = streaming
    tg_config.typing_indicator = typing_indicator

    def effective_config_for_bot(name):
        if bots and name in bots:
            bot_cfg = bots[name]
            return bot_cfg
        return tg_config

    tg_config.effective_config_for_bot = effective_config_for_bot

    channels = MagicMock()
    channels.telegram = tg_config

    config = MagicMock()
    config.channels = channels
    config.security = MagicMock()
    config.security.denied_users = []
    config.security.allowed_users = []
    config.agents = MagicMock()
    config.agents.agents = {}
    return config


def _make_message(
    user_id=12345,
    chat_id=12345,
    text="hello",
    message_id=1,
    first_name="Alice",
    message_thread_id=None,
):
    """Create a mock Telegram message."""
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = first_name
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = message_id
    msg.message_thread_id = message_thread_id
    return msg


def _make_plugin():
    """Create a TelegramPlugin with a mock bot pre-loaded."""
    plugin = TelegramPlugin()
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    bot.edit_message_text = AsyncMock()
    bot.send_chat_action = AsyncMock()
    plugin._bots = {"_default": bot}
    plugin._chat_ids = {"_default": None}
    return plugin, bot


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    async def test_allowed_user_passes(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(check_access=True)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(user_id=111), "_default", bot)
        handle.dispatch.assert_called_once()

    async def test_denied_user_blocked(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(check_access=False)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(user_id=999), "_default", bot)
        handle.dispatch.assert_not_called()
        bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDedup:

    async def test_duplicate_message_dropped(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(is_duplicate=True)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)
        handle.dispatch.assert_not_called()

    async def test_non_duplicate_processed(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(is_duplicate=False)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Agent routing (multi-bot)
# ---------------------------------------------------------------------------

class TestMultiBotRouting:

    def test_agent_id_for_bot_uses_configured_agent(self):
        plugin = TelegramPlugin()
        bot_cfg = MagicMock()
        bot_cfg.agent = "ritchie"
        bot_cfg.bot_token = "tok"

        config = _make_config(bots={"main": bot_cfg})
        handle = _make_gw_handle(config=config, agent_id="ritchie")
        plugin._gw = handle
        plugin._telegram_config = config.channels.telegram

        result = plugin._agent_id_for_bot("main")
        handle.resolve_agent_id.assert_called_with("ritchie")
        assert result == "ritchie"

    def test_agent_id_for_bot_falls_back(self):
        plugin = TelegramPlugin()
        config = _make_config()
        handle = _make_gw_handle(config=config, agent_id="default_agent")
        plugin._gw = handle
        plugin._telegram_config = config.channels.telegram

        result = plugin._agent_id_for_bot("_default")
        handle.resolve_agent_id.assert_called_with()
        assert result == "default_agent"


# ---------------------------------------------------------------------------
# Non-streaming response
# ---------------------------------------------------------------------------

class TestNonStreaming:

    async def test_dispatch_called_and_response_sent(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(dispatch_return="Hello from agent")
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)

        handle.dispatch.assert_called_once()
        bot.send_message.assert_called()
        # Last call should be the response (not typing)
        calls = bot.send_message.call_args_list
        assert any("Hello from agent" in str(c) for c in calls)

    async def test_no_response_means_no_send(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(dispatch_return=None)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)

        handle.dispatch.assert_called_once()
        # Only typing action called, not send_message with text
        for call in bot.send_message.call_args_list:
            assert "chat_id" not in call.kwargs or "Sorry" not in str(call)


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestCommandDispatch:

    async def test_slash_command_intercepted(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(command_return="Command executed!")
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(text="/help"), "_default", bot)

        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_not_called()  # Command short-circuits
        bot.send_message.assert_called()

    async def test_unrecognized_command_falls_through(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle(command_return=None)
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(_make_message(text="/unknown"), "_default", bot)

        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_called_once()  # Falls through to agent


# ---------------------------------------------------------------------------
# Endpoint registration
# ---------------------------------------------------------------------------

class TestEndpointRegistration:

    async def test_endpoint_registered_before_dispatch(self):
        plugin, bot = _make_plugin()
        handle = _make_gw_handle()
        plugin._gw = handle
        plugin._telegram_config = handle.config.channels.telegram

        await plugin._handle_message(
            _make_message(user_id=111, chat_id=222), "_default", bot
        )

        handle.register_endpoint.assert_called_once_with(
            handle.resolve_agent_id(),
            "telegram",
            {"sender_id": "222", "sender": "Alice", "bot_name": "_default"},
        )


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------

class TestTypingIndicator:

    async def test_typing_sent_when_enabled(self):
        plugin, bot = _make_plugin()
        config = _make_config(typing_indicator=True)
        handle = _make_gw_handle(config=config)
        plugin._gw = handle
        plugin._telegram_config = config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)

        bot.send_chat_action.assert_called()

    async def test_typing_not_sent_when_disabled(self):
        plugin, bot = _make_plugin()
        config = _make_config(typing_indicator=False)
        handle = _make_gw_handle(config=config)
        plugin._gw = handle
        plugin._telegram_config = config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)

        bot.send_chat_action.assert_not_called()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

class TestStreaming:

    async def test_streaming_passes_on_chunk_to_dispatch(self):
        plugin, bot = _make_plugin()
        config = _make_config(streaming=True)
        handle = _make_gw_handle(config=config, dispatch_return="full response")
        plugin._gw = handle
        plugin._telegram_config = config.channels.telegram

        await plugin._handle_message(_make_message(), "_default", bot)

        # dispatch should have been called with on_chunk set
        call_kwargs = handle.dispatch.call_args.kwargs
        assert call_kwargs.get("on_chunk") is not None


# ---------------------------------------------------------------------------
# send_message / edit_message / send_typing
# ---------------------------------------------------------------------------

class TestOutbound:

    async def test_send_message(self):
        plugin, bot = _make_plugin()
        target = MessageTarget(channel="telegram", user_id="12345")
        result = await plugin.send_message(target, "hello")
        bot.send_message.assert_called_once_with(chat_id="12345", text="hello")
        assert result == "99"

    async def test_send_message_with_parse_mode(self):
        plugin, bot = _make_plugin()
        target = MessageTarget(channel="telegram", user_id="12345")
        await plugin.send_message(target, "<b>bold</b>", parse_mode="HTML")
        bot.send_message.assert_called_once_with(
            chat_id="12345", text="<b>bold</b>", parse_mode="HTML"
        )

    async def test_send_message_with_bot_name(self):
        plugin = TelegramPlugin()
        bot1 = AsyncMock()
        bot1.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        bot2 = AsyncMock()
        bot2.send_message = AsyncMock(return_value=MagicMock(message_id=2))
        plugin._bots = {"main": bot1, "support": bot2}

        target = MessageTarget(channel="telegram", user_id="12345")
        await plugin.send_message(target, "hi", bot_name="support")
        bot2.send_message.assert_called_once()
        bot1.send_message.assert_not_called()

    async def test_edit_message(self):
        plugin, bot = _make_plugin()
        target = MessageTarget(channel="telegram", user_id="12345")
        await plugin.edit_message(target, "99", "updated text")
        bot.edit_message_text.assert_called_once_with(
            chat_id="12345", message_id=99, text="updated text"
        )

    async def test_send_typing(self):
        plugin, bot = _make_plugin()
        target = MessageTarget(channel="telegram", user_id="12345")
        await plugin.send_typing(target)
        bot.send_chat_action.assert_called_once_with(
            chat_id="12345", action="typing"
        )


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class TestStop:

    async def test_stop_cancels_polling_tasks(self):
        plugin = TelegramPlugin()

        async def _sleeper():
            await asyncio.sleep(999)

        task1 = asyncio.create_task(_sleeper())
        # task2 — already done
        task2 = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)  # let task2 finish

        plugin._polling_tasks = {"bot1": task1, "bot2": task2}
        plugin._bots = {"bot1": MagicMock(), "bot2": MagicMock()}
        plugin._chat_ids = {"bot1": None, "bot2": None}

        await plugin.stop()

        assert task1.cancelled()
        assert plugin._polling_tasks == {}
        assert plugin._bots == {}

    async def test_stop_with_no_tasks(self):
        plugin = TelegramPlugin()
        plugin._polling_tasks = {}
        plugin._bots = {}
        plugin._chat_ids = {}
        await plugin.stop()  # Should not raise


# ---------------------------------------------------------------------------
# _live_display helper
# ---------------------------------------------------------------------------

class TestLiveDisplay:

    def test_strips_complete_thinking_blocks(self):
        text = "<think>reasoning here</think>The answer is 42."
        assert _live_display(text) == "The answer is 42."

    def test_hides_incomplete_thinking_block(self):
        text = "prefix <think>partial reasoning"
        result = _live_display(text)
        assert "partial reasoning" not in result
        assert result == "prefix"

    def test_returns_plain_text_unchanged(self):
        assert _live_display("just text") == "just text"

    def test_empty_string(self):
        assert _live_display("") == ""


# ---------------------------------------------------------------------------
# Bot resolution
# ---------------------------------------------------------------------------

class TestBotResolution:

    def test_resolve_bot_by_name(self):
        plugin = TelegramPlugin()
        bot1, bot2 = MagicMock(), MagicMock()
        plugin._bots = {"main": bot1, "support": bot2}
        assert plugin._resolve_bot("support") is bot2

    def test_resolve_bot_fallback(self):
        plugin = TelegramPlugin()
        bot = MagicMock()
        plugin._bots = {"_default": bot}
        assert plugin._resolve_bot() is bot

    def test_resolve_bot_none(self):
        plugin = TelegramPlugin()
        plugin._bots = {}
        assert plugin._resolve_bot() is None
