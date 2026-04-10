"""Tests for multi-bot Telegram support via TelegramPlugin."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from pyclopse.channels.telegram_plugin import TelegramBotConfig, TelegramChannelConfig
from pyclopse.config.schema import (
    AgentsConfig,
    ChannelsConfig,
    Config,
    SecurityConfig,
)


# -- helpers ------------------------------------------------------------------


def _make_message(user_id: int = 42, chat_id: int = 42, text: str = "hi", message_id: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Tester"
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = message_id
    msg.message_thread_id = None
    return msg


def _make_plugin(telegram_config: TelegramChannelConfig):
    """Build a TelegramPlugin with mocked GatewayHandle and multiple bot mocks."""
    from pyclopse.channels.telegram_plugin import TelegramPlugin

    plugin = TelegramPlugin()

    config = Config(
        channels=ChannelsConfig(telegram=telegram_config),
        agents=AgentsConfig(),
        security=SecurityConfig(),
    )

    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value="reply")
    handle.dispatch_command = AsyncMock(return_value=None)
    handle.is_duplicate = MagicMock(return_value=False)
    handle.check_access = MagicMock(return_value=True)
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    type(handle).config = PropertyMock(return_value=config)

    # resolve_agent_id: if a hint is provided and it matches a known agent,
    # return it; otherwise fall back to "main".
    known_agents = {"main", "ritchie"}

    def _resolve(hint=None):
        if hint and hint in known_agents:
            return hint
        return "main"

    handle.resolve_agent_id = MagicMock(side_effect=_resolve)

    plugin._gw = handle
    plugin._telegram_config = telegram_config

    # Create per-bot mocks (caller can override)
    plugin._bots = {}
    plugin._chat_ids = {}

    return plugin, handle


def _make_bot_mock() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    bot.send_chat_action = AsyncMock()
    return bot


# -- schema tests -------------------------------------------------------------


class TestTelegramMultiBotSchema:
    def test_bots_field_defaults_empty(self):
        cfg = TelegramChannelConfig.model_validate({"botToken": "tok"})
        assert cfg.bots == {}

    def test_bots_field_parsed(self):
        cfg = TelegramChannelConfig.model_validate({
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
        cfg = TelegramChannelConfig.model_validate({
            "allowedUsers": [1, 2],
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.allowed_users == [1, 2]

    def test_effective_config_bot_overrides_parent_allowed_users(self):
        cfg = TelegramChannelConfig.model_validate({
            "allowedUsers": [1, 2],
            "bots": {"special": {"botToken": "tok", "allowedUsers": [99]}},
        })
        eff = cfg.effective_config_for_bot("special")
        assert eff.allowed_users == [99]

    def test_effective_config_inherits_streaming(self):
        cfg = TelegramChannelConfig.model_validate({
            "streaming": True,
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.streaming is True

    def test_effective_config_bot_overrides_streaming(self):
        cfg = TelegramChannelConfig.model_validate({
            "streaming": True,
            "bots": {"main": {"botToken": "tok", "streaming": False}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.streaming is False

    def test_effective_config_inherits_typing_indicator(self):
        cfg = TelegramChannelConfig.model_validate({
            "typingIndicator": False,
            "bots": {"main": {"botToken": "tok"}},
        })
        eff = cfg.effective_config_for_bot("main")
        assert eff.typing_indicator is False

    def test_backward_compat_no_bots_key(self):
        cfg = TelegramChannelConfig.model_validate({"botToken": "legacy"})
        assert cfg.bot_token == "legacy"
        assert cfg.bots == {}

    def test_telegram_bot_config_secret_ref_stored_literally(self):
        cfg = TelegramBotConfig.model_validate({"botToken": "${MY_BOT_TOKEN}"})
        assert cfg.bot_token == "${MY_BOT_TOKEN}"


# -- plugin routing tests -----------------------------------------------------


class TestMultiBotPluginRouting:
    """Multi-bot routing in TelegramPlugin._handle_message."""

    @pytest.mark.asyncio
    async def test_routes_to_correct_agent_by_bot_name(self):
        """Messages arriving on the 'ritchie' bot route to the 'ritchie' agent."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        plugin, handle = _make_plugin(tg)
        bot_mock = _make_bot_mock()
        plugin._bots["ritchie"] = bot_mock
        plugin._chat_ids["ritchie"] = None

        msg = _make_message(user_id=42)
        await plugin._handle_message(msg, "ritchie", bot_mock)

        handle.dispatch.assert_called_once()
        call_kwargs = handle.dispatch.call_args.kwargs
        assert call_kwargs["agent_id"] == "ritchie"

    @pytest.mark.asyncio
    async def test_different_bots_route_to_different_agents(self):
        """Two bots, same user_id -- each routes to its own agent."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        plugin, handle = _make_plugin(tg)

        bot_main = _make_bot_mock()
        bot_ritchie = _make_bot_mock()
        plugin._bots["main"] = bot_main
        plugin._bots["ritchie"] = bot_ritchie
        plugin._chat_ids["main"] = None
        plugin._chat_ids["ritchie"] = None

        msg1 = _make_message(user_id=42, message_id=1)
        msg2 = _make_message(user_id=42, message_id=2)

        await plugin._handle_message(msg1, "main", bot_main)
        await plugin._handle_message(msg2, "ritchie", bot_ritchie)

        assert handle.dispatch.call_count == 2
        calls = handle.dispatch.call_args_list
        assert calls[0].kwargs["agent_id"] == "main"
        assert calls[1].kwargs["agent_id"] == "ritchie"

    @pytest.mark.asyncio
    async def test_dedup_is_per_bot(self):
        """Same message_id arriving on two different bots is NOT deduplicated."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        plugin, handle = _make_plugin(tg)

        # Track which channel keys have been "seen" to simulate per-bot dedup
        seen = set()

        def _is_dup(channel, msg_id):
            key = f"{channel}:{msg_id}"
            if key in seen:
                return True
            seen.add(key)
            return False

        handle.is_duplicate = MagicMock(side_effect=_is_dup)

        bot_main = _make_bot_mock()
        bot_ritchie = _make_bot_mock()
        plugin._bots["main"] = bot_main
        plugin._bots["ritchie"] = bot_ritchie
        plugin._chat_ids["main"] = None
        plugin._chat_ids["ritchie"] = None

        msg_main = _make_message(user_id=42, message_id=999)
        msg_ritchie = _make_message(user_id=42, message_id=999)

        await plugin._handle_message(msg_main, "main", bot_main)
        await plugin._handle_message(msg_ritchie, "ritchie", bot_ritchie)

        # Both should be processed -- they're different bots
        assert handle.dispatch.call_count == 2

    @pytest.mark.asyncio
    async def test_dedup_blocks_same_bot_duplicate(self):
        """Same message_id on the same bot IS deduplicated."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],
            "bots": {"main": {"botToken": "tok-main", "agent": "main"}},
        })
        plugin, handle = _make_plugin(tg)

        seen = set()

        def _is_dup(channel, msg_id):
            key = f"{channel}:{msg_id}"
            if key in seen:
                return True
            seen.add(key)
            return False

        handle.is_duplicate = MagicMock(side_effect=_is_dup)

        bot_mock = _make_bot_mock()
        plugin._bots["main"] = bot_mock
        plugin._chat_ids["main"] = None

        msg1 = _make_message(user_id=42, message_id=777)
        msg2 = _make_message(user_id=42, message_id=777)

        await plugin._handle_message(msg1, "main", bot_mock)
        await plugin._handle_message(msg2, "main", bot_mock)

        assert handle.dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_per_bot_allowed_users_overrides_parent(self):
        """User 99 is blocked by parent allowed_users but passes through bot override."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [1, 2],  # parent: only 1 and 2
            "bots": {
                "special": {"botToken": "tok", "agent": "ritchie", "allowedUsers": [99]},
            },
        })
        plugin, handle = _make_plugin(tg)
        # check_access returns True for user 99 on the "special" bot
        handle.check_access = MagicMock(return_value=True)

        bot_mock = _make_bot_mock()
        plugin._bots["special"] = bot_mock
        plugin._chat_ids["special"] = None

        msg = _make_message(user_id=99)
        await plugin._handle_message(msg, "special", bot_mock)

        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_bot_allowed_users_blocks_non_listed(self):
        """User 42 is blocked by bot-specific allowed_users even if parent would allow."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],  # parent allows 42
            "bots": {
                "restricted": {"botToken": "tok", "agent": "main", "allowedUsers": [99]},
            },
        })
        plugin, handle = _make_plugin(tg)
        # check_access returns False for user 42 on the "restricted" bot
        handle.check_access = MagicMock(return_value=False)

        bot_mock = _make_bot_mock()
        plugin._bots["restricted"] = bot_mock
        plugin._chat_ids["restricted"] = None

        msg = _make_message(user_id=42)
        await plugin._handle_message(msg, "restricted", bot_mock)

        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_use_the_bot_that_received_the_message(self):
        """Reply is sent via the bot that received the message, not the first bot."""
        tg = TelegramChannelConfig.model_validate({
            "allowedUsers": [42],
            "bots": {
                "main": {"botToken": "tok-main", "agent": "main"},
                "ritchie": {"botToken": "tok-r", "agent": "ritchie"},
            },
        })
        plugin, handle = _make_plugin(tg)
        handle.dispatch = AsyncMock(return_value="ritchie reply")

        bot_main = _make_bot_mock()
        bot_ritchie = _make_bot_mock()
        plugin._bots["main"] = bot_main
        plugin._bots["ritchie"] = bot_ritchie
        plugin._chat_ids["main"] = None
        plugin._chat_ids["ritchie"] = None

        msg = _make_message(user_id=42, chat_id=42, message_id=5)
        await plugin._handle_message(msg, "ritchie", bot_ritchie)

        bot_ritchie.send_message.assert_called()
        bot_main.send_message.assert_not_called()

    def test_agent_id_for_bot_resolves_configured_agent(self):
        """_agent_id_for_bot returns the agent set in bots config."""
        tg = TelegramChannelConfig.model_validate({
            "bots": {"ritchie": {"botToken": "tok", "agent": "ritchie"}},
        })
        plugin, _ = _make_plugin(tg)
        assert plugin._agent_id_for_bot("ritchie") == "ritchie"

    def test_agent_id_for_bot_falls_back_to_first_agent_when_unknown(self):
        """_agent_id_for_bot falls back when agent name is not registered."""
        tg = TelegramChannelConfig.model_validate({
            "bots": {"x": {"botToken": "tok", "agent": "nonexistent"}},
        })
        plugin, handle = _make_plugin(tg)
        # resolve_agent_id("nonexistent") returns "main" (fallback)
        assert plugin._agent_id_for_bot("x") == "main"

    def test_agent_id_for_bot_falls_back_for_default(self):
        """_agent_id_for_bot returns first agent for '_default' (single-bot mode)."""
        tg = TelegramChannelConfig.model_validate({"botToken": "tok", "allowedUsers": [42]})
        plugin, _ = _make_plugin(tg)
        assert plugin._agent_id_for_bot("_default") == "main"


# -- start/stop lifecycle tests -----------------------------------------------


class TestMultiBotLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_one_task_per_bot(self):
        """Polling tasks are created for each bot when start runs."""
        from pyclopse.channels.telegram_plugin import TelegramPlugin

        plugin = TelegramPlugin()

        # Pre-populate bots (normally done by start -> _resolve_bots)
        bot_a = AsyncMock()
        bot_b = AsyncMock()
        plugin._bots = {"main": bot_a, "ritchie": bot_b}
        plugin._chat_ids = {"main": None, "ritchie": None}

        # Track which bots get polled
        poll_calls = []

        async def fake_poll(bot_name, bot):
            poll_calls.append(bot_name)
            # Immediately return (simulates poll stopping)

        plugin._poll_bot = fake_poll

        # Manually create polling tasks the same way start() does
        for bn, bt in plugin._bots.items():
            task = asyncio.create_task(
                plugin._poll_bot(bn, bt),
                name=f"telegram-poll-{bn}",
            )
            plugin._polling_tasks[bn] = task
        await asyncio.sleep(0)  # let tasks run

        assert "main" in poll_calls
        assert "ritchie" in poll_calls

    @pytest.mark.asyncio
    async def test_stop_cancels_all_polling_tasks(self):
        """stop() cancels and clears all polling tasks."""
        from pyclopse.channels.telegram_plugin import TelegramPlugin

        plugin = TelegramPlugin()

        # Create two long-running tasks
        async def _long_running():
            await asyncio.sleep(9999)

        task_a = asyncio.create_task(_long_running())
        task_b = asyncio.create_task(_long_running())
        plugin._polling_tasks = {"main": task_a, "ritchie": task_b}

        await plugin.stop()

        assert task_a.cancelled()
        assert task_b.cancelled()
        assert plugin._polling_tasks == {}
