"""
Tests for Telegram typing indicator in _handle_telegram_message.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, call


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
    return msg


def _make_gateway(typing_indicator=True, handle_message_response="ok"):
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
    gw._active_tasks = {}
    gw._channels = {}
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    gw._command_registry = MagicMock()
    gw._command_registry.dispatch = AsyncMock(return_value=None)

    telegram_cfg = TelegramConfig.model_validate({
        "enabled": True,
        "botToken": "fake",
        "allowedUsers": [111],
        "typingIndicator": typing_indicator,
    })
    channels_cfg = ChannelsConfig(telegram=telegram_cfg)
    security_cfg = SecurityConfig()
    gw._config = Config(channels=channels_cfg, agents=AgentsConfig(), security=security_cfg)

    gw.enqueue_message = AsyncMock(return_value=handle_message_response)
    gw.handle_message = AsyncMock(return_value=handle_message_response)
    mock_sm = MagicMock()
    mock_sm.get_or_create_session = AsyncMock(return_value=MagicMock())
    gw._session_manager = mock_sm
    gw._agent_manager = MagicMock()
    gw._agent_manager.agents = {}

    return gw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTypingIndicator:

    @pytest.mark.asyncio
    async def test_send_chat_action_called_when_enabled(self):
        gw = _make_gateway(typing_indicator=True)
        await gw._handle_telegram_message(_make_message())
        gw._telegram_bot.send_chat_action.assert_called()
        args = gw._telegram_bot.send_chat_action.call_args
        assert args.kwargs.get("action") == "typing" or args.args[-1] == "typing"

    @pytest.mark.asyncio
    async def test_send_chat_action_not_called_when_disabled(self):
        gw = _make_gateway(typing_indicator=False)
        await gw._handle_telegram_message(_make_message())
        gw._telegram_bot.send_chat_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_typing_uses_correct_chat_id(self):
        gw = _make_gateway(typing_indicator=True)
        msg = _make_message()
        msg.chat.id = 12345
        await gw._handle_telegram_message(msg)
        call_kwargs = gw._telegram_bot.send_chat_action.call_args.kwargs
        assert call_kwargs.get("chat_id") == "12345" or call_kwargs.get("chat_id") == 12345

    @pytest.mark.asyncio
    async def test_response_sent_even_with_typing(self):
        gw = _make_gateway(typing_indicator=True, handle_message_response="my response")
        await gw._handle_telegram_message(_make_message())
        gw._telegram_bot.send_message.assert_called_once()
        call_kwargs = gw._telegram_bot.send_message.call_args.kwargs
        assert call_kwargs.get("text") == "my response"

    @pytest.mark.asyncio
    async def test_typing_cancelled_after_response(self):
        """After handle_message returns, the typing loop must be cancelled."""
        gw = _make_gateway(typing_indicator=True)
        # We can't easily inspect the task directly, but we verify the method
        # completes without hanging (typing task is cleaned up).
        await asyncio.wait_for(
            gw._handle_telegram_message(_make_message()),
            timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_typing_cancelled_even_on_error(self):
        """Typing task is cancelled even when handle_message raises."""
        gw = _make_gateway(typing_indicator=True)
        gw.enqueue_message = AsyncMock(side_effect=RuntimeError("boom"))
        # Should not hang — typing task cleaned up in finally block
        await asyncio.wait_for(
            gw._handle_telegram_message(_make_message()),
            timeout=2.0,
        )
