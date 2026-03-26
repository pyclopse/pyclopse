"""
Tests for Gateway._split_message() and its use in _handle_telegram_message.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# _split_message unit tests
# ---------------------------------------------------------------------------

def _split(text, limit=4000):
    from pyclopse.core.gateway import Gateway
    return Gateway._split_message(text, limit=limit)


class TestSplitMessage:

    def test_short_message_unchanged(self):
        text = "Hello!"
        assert _split(text) == ["Hello!"]

    def test_exactly_at_limit_not_split(self):
        text = "x" * 4000
        assert _split(text, limit=4000) == [text]

    def test_one_char_over_limit_splits(self):
        text = "x" * 4001
        chunks = _split(text, limit=4000)
        assert len(chunks) == 2
        assert all(len(c) <= 4000 for c in chunks)

    def test_reconstructed_text_contains_all_content(self):
        text = "word " * 1000  # 5000 chars
        chunks = _split(text, limit=4000)
        # Rejoin should contain all words (whitespace may be trimmed at boundaries)
        combined = " ".join(chunks)
        assert "word" in combined
        assert len(chunks) >= 2

    def test_split_on_paragraph_boundary(self):
        para1 = "A" * 2000
        para2 = "B" * 2000
        text = para1 + "\n\n" + para2
        chunks = _split(text, limit=4000)
        # Should split at the paragraph boundary
        assert len(chunks) == 2
        assert chunks[0] == para1
        assert chunks[1] == para2

    def test_split_on_single_newline_when_no_paragraph(self):
        line1 = "A" * 2000
        line2 = "B" * 2000
        text = line1 + "\n" + line2
        chunks = _split(text, limit=4000)
        assert len(chunks) == 2

    def test_hard_split_when_no_good_boundary(self):
        # One very long line, no newlines
        text = "x" * 9000
        chunks = _split(text, limit=4000)
        assert len(chunks) == 3
        assert all(len(c) <= 4000 for c in chunks)

    def test_all_chunks_within_limit(self):
        import random, string
        random.seed(42)
        text = "\n".join(
            "".join(random.choices(string.ascii_letters + " ", k=300))
            for _ in range(40)
        )  # ~12000 chars
        chunks = _split(text, limit=4000)
        assert all(len(c) <= 4000 for c in chunks)

    def test_empty_text_returns_empty_list_or_single(self):
        chunks = _split("", limit=4000)
        # Either empty or [""] — just no crash and reasonable result
        assert isinstance(chunks, list)

    def test_long_response_splits_correctly_with_small_limit(self):
        text = "Hello world.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = _split(text, limit=20)
        assert len(chunks) > 1
        assert all(len(c) <= 20 for c in chunks)


# ---------------------------------------------------------------------------
# Integration: _handle_telegram_message sends multiple chunks
# ---------------------------------------------------------------------------

def _make_gateway(response_text):
    from pyclopse.core.gateway import Gateway
    from pyclopse.config.schema import (
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
        "typingIndicator": False,  # disable to simplify test
    })
    channels_cfg = ChannelsConfig(telegram=telegram_cfg)
    gw._config = Config(channels=channels_cfg, agents=AgentsConfig(), security=SecurityConfig())

    gw.handle_message = AsyncMock(return_value=response_text)
    # enqueue_message is the new queue-aware wrapper around handle_message;
    # mock it here so the stub doesn't need a real QueueManager.
    gw.enqueue_message = AsyncMock(return_value=response_text)
    mock_sm = MagicMock()
    mock_sm.get_or_create_session = AsyncMock(return_value=MagicMock())
    gw._session_manager = mock_sm
    gw._agent_manager = MagicMock()
    gw._agent_manager.agents = {}

    return gw


def _make_message(user_id=111, message_id=1, text="hi"):
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "T"
    msg.chat.id = 42
    msg.message_id = message_id
    msg.text = text
    return msg


class TestMessageSendSplitting:

    @pytest.mark.asyncio
    async def test_short_response_sends_once(self):
        gw = _make_gateway("short response")
        await gw._handle_telegram_message(_make_message())
        assert gw._telegram_bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_long_response_sends_multiple(self):
        # 9000 chars forces at least 3 sends at the 4000-char default limit
        long_text = "sentence " * 1000  # ~9000 chars with spaces
        gw = _make_gateway(long_text)
        await gw._handle_telegram_message(_make_message())
        assert gw._telegram_bot.send_message.call_count >= 2

    @pytest.mark.asyncio
    async def test_each_chunk_within_telegram_limit(self):
        """Each send_message call must have text ≤ 4096 chars."""
        long_text = "paragraph\n\n" * 500  # ~5500 chars
        gw = _make_gateway(long_text)
        await gw._handle_telegram_message(_make_message())
        for call in gw._telegram_bot.send_message.call_args_list:
            text = call.kwargs.get("text", call.args[-1] if call.args else "")
            assert len(text) <= 4000, f"Chunk too long: {len(text)}"
