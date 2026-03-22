"""Tests for multi-bot Telegram support (channels.telegram.bots)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclaw.config.schema import (
    AgentsConfig,
    ChannelsConfig,
    Config,
    TelegramBotConfig,
    TelegramConfig,
)
from pyclaw.core.gateway import Gateway


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_message(user_id: int = 42, chat_id: int = 42, text: str = "hi", message_id: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Tester"
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = message_id
    return msg


def _make_gateway(telegram_config: TelegramConfig) -> Gateway:
    """Build a minimal Gateway stub (no __init__) with the given Telegram config."""
    gw = Gateway.__new__(Gateway)
    gw._is_running = True
    gw._initialized = True
    gw._logger = MagicMock()
    gw._audit_logger = None
    gw._active_tasks = {}
    gw._session_manager = None
    gw._channels = {}
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    gw._hook_registry = None
    gw._known_session_ids = set()
    gw._usage = {"messages_total": 0, "messages_by_channel": {}, "messages_by_agent": {}}

    # Multi-bot dicts (normally set in __init__)
    gw._tg_bots = {}
    gw._tg_chat_ids = {}
    gw._tg_polling_tasks = {}

    gw._config = Config(
        channels=ChannelsConfig(telegram=telegram_config),
        agents=AgentsConfig(),
    )

    # Minimal agent manager with two agents
    am = MagicMock()
    am.agents = {"main": MagicMock(), "ritchie": MagicMock()}
    am.get_agent.side_effect = lambda aid: am.agents.get(aid)
    gw._agent_manager = am

    # Security config
    sec = MagicMock()
    sec.denied_users = []
    sec.allowed_users = []
    gw._config.security = sec

    # Commands
    from pyclaw.core.commands import CommandRegistry
    gw._command_registry = CommandRegistry()

    return gw


# ── schema tests ──────────────────────────────────────────────────────────────


class TestTelegramMultiBotSchema:
    def test_bots_field_defaults_empty(self):
        cfg = TelegramConfig.model_validate({"botToken": "tok"})
        assert cfg.bots == {}

    def test_bots_field_parsed(self):
        cfg = TelegramConfig.model_validate({
            "allowedUsers": [1, 2],
            "bots": {
                "main": {"botToken": "tok-main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie", "allowedUsers": [99]},
            },
        })
        assert len(cfg.bots) == 2
        assert cfg.bots["ritchie"].agent == "ritchie"
        assert cfg.bots["main"].bot_token == "tok-main"

    def test_effective_config_inherits_parent_allowed_users(self):
        cfg = TelegramConfig.model_validate({
            "allowedUsers": [1, 2],
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.allowed_users == [1, 2]

    def test_effective_config_bot_overrides_parent_allowed_users(self):
        cfg = TelegramConfig.model_validate({
            "allowedUsers": [1, 2],
            "bots": {"special": {"botToken": "tok", "allowedUsers": [99]}},
        })
        eff = cfg.effective_config_for_bot("special")
        assert eff.allowed_users == [99]

    def test_effective_config_inherits_streaming(self):
        cfg = TelegramConfig.model_validate({
            "streaming": True,
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.streaming is True

    def test_effective_config_bot_overrides_streaming(self):
        cfg = TelegramConfig.model_validate({
            "streaming": True,
            "bots": {"main": {"botToken": "tok", "streaming": False}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.streaming is False

    def test_effective_config_inherits_typing_indicator(self):
        cfg = TelegramConfig.model_validate({
            "typingIndicator": False,
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.typing_indicator is False

    def test_backward_compat_no_bots_key(self):
        cfg = TelegramConfig.model_validate({"botToken": "legacy"})
        assert cfg.bot_token == "legacy"
        assert cfg.bots == {}

    def test_telegram_bot_config_secret_ref_stored_literally(self):
        # ${...} references are resolved by SecretsManager.resolve_raw() before
        # Pydantic validation runs (in ConfigLoader). Passing them directly to
        # model_validate stores the literal string — resolution is not the
        # model's responsibility.
        cfg = TelegramBotConfig.model_validate({"botToken": "${MY_BOT_TOKEN}"})
        assert cfg.bot_token == "${MY_BOT_TOKEN}"


# ── gateway routing tests ─────────────────────────────────────────────────────


class TestMultiBotGatewayRouting:
    """Multi-bot routing in _handle_telegram_message."""

    @pytest.mark.asyncio
    async def test_routes_to_correct_agent_by_bot_name(self):
        """Messages arriving on the 'ritchie' bot route to the 'ritchie' agent."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")
        bot_mock = AsyncMock()

        msg = _make_message(user_id=42)
        await gw._handle_telegram_message(msg, bot_name="ritchie", bot=bot_mock)

        gw.enqueue_message.assert_called_once()
        call_kwargs = gw.enqueue_message.call_args.kwargs
        assert call_kwargs["agent_id"] == "ritchie"

    @pytest.mark.asyncio
    async def test_different_bots_route_to_different_agents(self):
        """Two bots, same user_id — each routes to its own agent."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")

        bot_main = AsyncMock()
        bot_ritchie = AsyncMock()

        msg1 = _make_message(user_id=42, message_id=1)
        msg2 = _make_message(user_id=42, message_id=2)

        await gw._handle_telegram_message(msg1, bot_name="main", bot=bot_main)
        await gw._handle_telegram_message(msg2, bot_name="ritchie", bot=bot_ritchie)

        assert gw.enqueue_message.call_count == 2
        calls = gw.enqueue_message.call_args_list
        assert calls[0].kwargs["agent_id"] == "main"
        assert calls[1].kwargs["agent_id"] == "ritchie"

    @pytest.mark.asyncio
    async def test_dedup_is_per_bot(self):
        """Same message_id arriving on two different bots is NOT deduplicated."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")

        # Both messages have the same message_id=999
        msg_main = _make_message(user_id=42, message_id=999)
        msg_ritchie = _make_message(user_id=42, message_id=999)

        await gw._handle_telegram_message(msg_main, bot_name="main", bot=AsyncMock())
        await gw._handle_telegram_message(msg_ritchie, bot_name="ritchie", bot=AsyncMock())

        # Both should be processed — they're different bots
        assert gw.enqueue_message.call_count == 2

    @pytest.mark.asyncio
    async def test_dedup_blocks_same_bot_duplicate(self):
        """Same message_id on the same bot IS deduplicated."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],
            "bots": {"main": {"botToken": "tok-main", "agent": "main"}},
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")
        bot_mock = AsyncMock()

        msg1 = _make_message(user_id=42, message_id=777)
        msg2 = _make_message(user_id=42, message_id=777)

        await gw._handle_telegram_message(msg1, bot_name="main", bot=bot_mock)
        await gw._handle_telegram_message(msg2, bot_name="main", bot=bot_mock)

        assert gw.enqueue_message.call_count == 1

    @pytest.mark.asyncio
    async def test_per_bot_allowed_users_overrides_parent(self):
        """User 99 is blocked by parent allowed_users but passes through bot override."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [1, 2],  # parent: only 1 and 2
            "bots": {
                "special": {"botToken": "tok", "agent": "ritchie", "allowedUsers": [99]},
            },
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")

        msg = _make_message(user_id=99)
        await gw._handle_telegram_message(msg, bot_name="special", bot=AsyncMock())

        gw.enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_bot_allowed_users_blocks_non_listed(self):
        """User 42 is blocked by bot-specific allowed_users even if parent would allow."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],  # parent allows 42
            "bots": {
                "restricted": {"botToken": "tok", "agent": "main", "allowedUsers": [99]},
            },
        })
        gw = _make_gateway(tg)
        gw.enqueue_message = AsyncMock(return_value="reply")
        gw.handle_message = AsyncMock(return_value="reply")

        msg = _make_message(user_id=42)
        await gw._handle_telegram_message(msg, bot_name="restricted", bot=AsyncMock())

        gw.enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_use_the_bot_that_received_the_message(self):
        """Reply is sent via the bot that received the message, not the first bot."""
        tg = TelegramConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        gw = _make_gateway(tg)
        gw.handle_message = AsyncMock(return_value="ritchie reply")

        bot_main = AsyncMock()
        bot_ritchie = AsyncMock()

        msg = _make_message(user_id=42, chat_id=42, message_id=5)
        await gw._handle_telegram_message(msg, bot_name="ritchie", bot=bot_ritchie)

        bot_ritchie.send_message.assert_called_once()
        bot_main.send_message.assert_not_called()

    def test_agent_id_for_bot_resolves_configured_agent(self):
        """_agent_id_for_bot returns the agent set in bots config."""
        tg = TelegramConfig.model_validate({
            "bots": {"ritchie": {"botToken": "tok", "agent": "ritchie"}},
        })
        gw = _make_gateway(tg)
        assert gw._agent_id_for_bot("ritchie") == "ritchie"

    def test_agent_id_for_bot_falls_back_to_first_agent_when_unknown(self):
        """_agent_id_for_bot falls back when agent name is not registered."""
        tg = TelegramConfig.model_validate({
            "bots": {"x": {"botToken": "tok", "agent": "nonexistent"}},
        })
        gw = _make_gateway(tg)
        # First agent in the dict is "main"
        assert gw._agent_id_for_bot("x") == "main"

    def test_agent_id_for_bot_falls_back_for_default(self):
        """_agent_id_for_bot returns first agent for '_default' (single-bot mode)."""
        tg = TelegramConfig.model_validate({"botToken": "tok", "allowedUsers": [42]})
        gw = _make_gateway(tg)
        assert gw._agent_id_for_bot("_default") == "main"


# ── start/stop lifecycle tests ────────────────────────────────────────────────


class TestMultiBotLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_one_task_per_bot(self):
        """start() creates one polling task per bot in _tg_bots."""
        gw = Gateway.__new__(Gateway)
        gw._is_running = False
        gw._initialized = True
        gw._logger = MagicMock()
        gw._tg_bots = {}
        gw._tg_chat_ids = {}
        gw._tg_polling_tasks = {}

        # Pre-populate bots (normally done by _init_telegram)
        bot_a = AsyncMock()
        bot_b = AsyncMock()
        gw._tg_bots["main"] = bot_a
        gw._tg_bots["ritchie"] = bot_b
        gw._tg_chat_ids["main"] = None
        gw._tg_chat_ids["ritchie"] = None

        # Mock _telegram_poll_bot to avoid real polling
        poll_calls = []

        async def fake_poll(bot_name, bot):
            poll_calls.append(bot_name)
            # Immediately return (simulates poll stopping)

        gw._telegram_poll_bot = fake_poll
        gw.initialize = AsyncMock()

        # Run start in background, then stop it
        async def _run():
            gw._is_running = True
            for bn, bt in gw._tg_bots.items():
                task = asyncio.create_task(
                    gw._telegram_poll_bot(bn, bt),
                    name=f"telegram-poll-{bn}",
                )
                gw._tg_polling_tasks[bn] = task
            await asyncio.sleep(0)  # let tasks run

        await _run()
        assert "main" in poll_calls
        assert "ritchie" in poll_calls

    @pytest.mark.asyncio
    async def test_stop_cancels_all_polling_tasks(self):
        """stop() cancels and clears all polling tasks."""
        gw = Gateway.__new__(Gateway)
        gw._is_running = True
        gw._logger = MagicMock()
        gw._tg_bots = {}
        gw._tg_chat_ids = {}
        gw._tg_polling_tasks = {}
        gw._agent_manager = None
        gw._session_manager = None
        gw._job_scheduler = None
        gw._channels = {}
        gw._hook_registry = None
        gw._mcp_server_task = None
        gw._api_server_task = None
        gw._api_uvicorn_server = None

        # Create two long-running tasks
        async def _long_running():
            await asyncio.sleep(9999)

        task_a = asyncio.create_task(_long_running())
        task_b = asyncio.create_task(_long_running())
        gw._tg_polling_tasks["main"] = task_a
        gw._tg_polling_tasks["ritchie"] = task_b

        await gw.stop()

        assert task_a.cancelled()
        assert task_b.cancelled()
        assert gw._tg_polling_tasks == {}
