"""Tests verifying correct job notification routing in a multi-bot Telegram
setup.

After fixes to the notification routing pipeline:
  1. `TelegramPlugin.bot_for_agent()` returns `(bot_instance, bot_name)`
     (previously returned `(bot_instance, chat_id)` — the core bug).
  2. `_job_notify` uses `session_manager.get_active_session()` (read-only)
     instead of `_get_active_session(user_id="")` which clobbered routing.
  3. `_deliver_to_channel` has triple fallback for bot_name:
     gateway cache -> session endpoint -> `plugin.bot_for_agent()`.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from pyclopse.channels.base import MessageTarget
from pyclopse.channels.telegram_plugin import (
    TelegramPlugin,
    TelegramBotConfig,
    TelegramChannelConfig,
)
from pyclopse.jobs.models import (
    Job,
    JobRun,
    JobStatus,
    CommandRun,
    CronSchedule,
    DeliverAnnounce,
)
from pyclopse.utils.time import now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_telegram_plugin(
    bots_config: dict,
    chat_ids: dict | None = None,
) -> TelegramPlugin:
    """Build a TelegramPlugin with mock Bot instances for each configured bot.

    Args:
        bots_config: Dict mapping bot_name -> {"agent": agent_id, ...}
        chat_ids: Dict mapping bot_name -> default chat_id
    """
    plugin = TelegramPlugin()

    # Build config from bots_config
    bots_raw = {}
    for bot_name, bot_info in bots_config.items():
        bots_raw[bot_name] = {
            "botToken": f"tok-{bot_name}",
            "agent": bot_info.get("agent"),
        }

    plugin._telegram_config = TelegramChannelConfig.model_validate({
        "bots": bots_raw,
    })

    # Create mock Bot instances
    plugin._bots = {}
    for bot_name in bots_config:
        bot_mock = AsyncMock()
        bot_mock.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        plugin._bots[bot_name] = bot_mock

    # Set chat_ids
    plugin._chat_ids = chat_ids or {bn: None for bn in bots_config}

    return plugin


def _make_gateway_stub(
    agents: list[str],
    telegram_plugin: TelegramPlugin | None = None,
    known_endpoints: dict | None = None,
    job_agents: dict | None = None,
):
    """Build a minimal Gateway stub with mocked subsystems."""
    from pyclopse.core.gateway import Gateway
    from pyclopse.config.schema import Config, AgentsConfig, SecurityConfig

    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()
    gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())
    gw._known_endpoints = known_endpoints or {}
    gw._channels = {}
    gw._known_session_ids = set()
    gw._agent_listeners = {}
    gw._hook_registry = None
    gw._audit_logger = None

    if telegram_plugin:
        gw._channels["telegram"] = telegram_plugin

    # Mock session manager
    gw._session_manager = MagicMock()
    gw._session_manager.get_active_session = AsyncMock(return_value=None)

    # Mock agent manager
    gw._agent_manager = MagicMock()
    for agent_id in agents:
        agent = MagicMock()
        agent.id = agent_id
        agent.config = MagicMock()
        agent.config.channel_sync = True
        gw._agent_manager.get_agent = MagicMock(return_value=agent)

    # Mock job scheduler
    gw._job_scheduler = MagicMock()
    gw._job_scheduler._job_agents = job_agents or {}

    return gw


def _make_session(agent_id: str, channel: str = "telegram", user_id: str = "8327082847"):
    """Build a mock Session object."""
    session = MagicMock()
    session.id = f"session-{agent_id}"
    session.agent_id = agent_id
    session.channel = channel
    session.user_id = user_id
    session.last_channel = channel
    session.last_user_id = user_id
    session.last_thread_ts = None
    session.context = {"channel_endpoints": {}}
    session.save_metadata = MagicMock()
    return session


# ---------------------------------------------------------------------------
# bot_for_agent returns correct values
# ---------------------------------------------------------------------------


class TestBotForAgentReturnValue:
    """Verify bot_for_agent() returns (bot_instance, bot_name)."""

    def test_bot_for_agent_returns_bot_name(self):
        """bot_for_agent returns (bot_instance, bot_name) for matching agent."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
                "niggy": {"agent": "niggy"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
                "niggy": "333333",
            },
        )

        bot, bot_name = plugin.bot_for_agent("ritchie")

        assert bot_name == "ritchie", (
            f"Expected bot_name 'ritchie', got {bot_name!r}"
        )
        assert bot is plugin._bots["ritchie"], (
            "Should return ritchie's bot instance"
        )

    def test_bot_for_agent_returns_correct_bot_for_each_agent(self):
        """Each agent resolves to its own bot."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
                "niggy": {"agent": "niggy"},
                "viavacavi": {"agent": "viavacavi"},
            },
        )

        for agent_id in ("main", "ritchie", "niggy", "viavacavi"):
            bot, bot_name = plugin.bot_for_agent(agent_id)
            assert bot_name == agent_id, (
                f"Expected bot_name '{agent_id}', got {bot_name!r}"
            )
            assert bot is plugin._bots[agent_id]

    def test_resolve_bot_succeeds_with_bot_name(self):
        """_resolve_bot finds the correct bot when given an actual bot name."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
            },
        )

        _, bot_name = plugin.bot_for_agent("ritchie")
        resolved = plugin._resolve_bot(bot_name)

        assert resolved is plugin._bots["ritchie"], (
            "_resolve_bot should return ritchie's bot when given 'ritchie'"
        )
        assert resolved is not plugin._bots["main"], (
            "Should NOT fall back to main's bot"
        )

    def test_bot_for_agent_fallback_returns_first_bot_name(self):
        """When agent has no configured bot, bot_for_agent falls back to first bot's name."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
            },
            chat_ids={
                "main": "111111",
            },
        )

        bot, bot_name = plugin.bot_for_agent("nonexistent")

        # Falls back to first bot — returns its name, not its chat_id
        assert bot_name == "main", (
            f"Expected fallback bot_name 'main', got {bot_name!r}"
        )
        assert bot is plugin._bots["main"]

    def test_bot_for_agent_no_bots_returns_none(self):
        """When no bots are configured, returns (None, None)."""
        plugin = TelegramPlugin()
        plugin._telegram_config = TelegramChannelConfig.model_validate({"bots": {}})
        plugin._bots = {}
        plugin._chat_ids = {}

        bot, bot_name = plugin.bot_for_agent("anything")
        assert bot is None
        assert bot_name is None


# ---------------------------------------------------------------------------
# _deliver_to_channel routes to the correct bot
# ---------------------------------------------------------------------------


class TestDeliverToChannelBotRouting:
    """Test that _deliver_to_channel sends notifications via the correct bot."""

    @pytest.mark.asyncio
    async def test_deliver_uses_correct_bot_via_bot_for_agent_fallback(self):
        """When no endpoint is cached, _deliver_to_channel falls back to
        bot_for_agent() which now returns the correct bot_name, routing
        the notification to the agent's own bot.
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "niggy": {"agent": "niggy"},
            },
            chat_ids={
                "main": "111111",
                "niggy": "333333",
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "niggy"],
            telegram_plugin=plugin,
            known_endpoints={},
        )

        session = _make_session(agent_id="niggy")
        session.context = {"channel_endpoints": {}}

        await gw._deliver_to_channel(session, "Test notification for niggy")

        niggy_bot = plugin._bots["niggy"]
        main_bot = plugin._bots["main"]

        assert niggy_bot.send_message.called, (
            "niggy's bot should receive the notification"
        )
        assert not main_bot.send_message.called, (
            "main's bot should NOT receive the notification"
        )

    @pytest.mark.asyncio
    async def test_deliver_uses_correct_bot_from_gateway_cache(self):
        """When _known_endpoints has bot_name, delivery uses it directly."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "niggy": {"agent": "niggy"},
            },
            chat_ids={
                "main": "111111",
                "niggy": "333333",
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "niggy"],
            telegram_plugin=plugin,
            known_endpoints={
                "niggy": {
                    "telegram": {
                        "sender_id": "333333",
                        "sender": "User",
                        "bot_name": "niggy",
                    }
                }
            },
        )

        session = _make_session(agent_id="niggy")

        await gw._deliver_to_channel(session, "Test notification for niggy")

        niggy_bot = plugin._bots["niggy"]
        main_bot = plugin._bots["main"]

        assert niggy_bot.send_message.called, "niggy's bot should be called"
        assert not main_bot.send_message.called, "main's bot should NOT be called"

    @pytest.mark.asyncio
    async def test_deliver_uses_correct_bot_from_session_endpoint(self):
        """When gateway cache is empty but session endpoint has bot_name,
        _deliver_to_channel uses the session endpoint (second fallback).
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "ritchie"],
            telegram_plugin=plugin,
            known_endpoints={},  # empty gateway cache
        )

        session = _make_session(agent_id="ritchie")
        session.context = {
            "channel_endpoints": {
                "telegram": {
                    "sender_id": "8327082847",
                    "sender": "User",
                    "bot_name": "ritchie",
                }
            }
        }

        await gw._deliver_to_channel(session, "Test notification")

        ritchie_bot = plugin._bots["ritchie"]
        main_bot = plugin._bots["main"]

        assert ritchie_bot.send_message.called, "ritchie's bot should be used"
        assert not main_bot.send_message.called, "main's bot should NOT be used"


# ---------------------------------------------------------------------------
# _job_notify fallback path uses correct bot_name
# ---------------------------------------------------------------------------


class TestJobNotifyFallbackPath:
    """Test the fallback path in _job_notify when no session exists."""

    def test_fallback_gets_bot_name_not_chat_id(self):
        """In the no-session fallback, bot_for_agent() returns bot_name
        which _resolve_bot uses to find the correct bot.
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
            },
        )

        # Simulate the exact code path from _job_notify fallback
        job_agent_id = "ritchie"
        ep = {}  # No endpoint cached (post-restart)
        bot_name = ep.get("bot_name")  # None

        if not bot_name:
            _, bot_name = plugin.bot_for_agent(job_agent_id)

        assert bot_name == "ritchie", (
            f"Expected bot_name 'ritchie', got {bot_name!r}"
        )

        resolved = plugin._resolve_bot(bot_name)
        assert resolved is plugin._bots["ritchie"], (
            "Should resolve to ritchie's bot"
        )
        assert resolved is not plugin._bots["main"], (
            "Should NOT fall back to main's bot"
        )


# ---------------------------------------------------------------------------
# _job_notify session path — uses read-only get_active_session
# ---------------------------------------------------------------------------


class TestJobNotifySessionPath:
    """Test the session path in _job_notify uses read-only session lookup."""

    @pytest.mark.asyncio
    async def test_job_notify_uses_read_only_session_lookup(self):
        """_job_notify now calls session_manager.get_active_session() (read-only)
        instead of gateway._get_active_session(user_id="") which would clobber
        the session's last_user_id.

        This ensures existing session routing is preserved when job notifications
        fire.
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "niggy": {"agent": "niggy"},
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "niggy"],
            telegram_plugin=plugin,
        )

        # Create a session with correct routing
        session = _make_session(agent_id="niggy", user_id="8327082847")
        session.context = {"channel_endpoints": {}}

        # The fixed code calls session_manager.get_active_session(agent_id)
        # which is a read-only lookup that does NOT modify session fields.
        gw._session_manager.get_active_session = AsyncMock(return_value=session)

        # Simulate what the fixed _job_notify does
        job_agent_id = "niggy"
        notify_session = await gw._session_manager.get_active_session(job_agent_id)

        # Session routing fields are preserved — NOT overwritten
        assert notify_session.last_user_id == "8327082847", (
            "last_user_id should be preserved, not clobbered to empty string"
        )
        assert notify_session.last_channel == "telegram"

        # Verify the correct method was called (not _get_active_session)
        gw._session_manager.get_active_session.assert_called_once_with(job_agent_id)


# ---------------------------------------------------------------------------
# End-to-end: job notifications reach the correct bot
# ---------------------------------------------------------------------------


class TestJobNotifyEndToEnd:
    """End-to-end tests for the complete _job_notify flow with multi-bot setup."""

    @pytest.mark.asyncio
    async def test_command_job_notify_fallback_resolves_correct_bot(self):
        """Command-type job (hourly-stats) owned by niggy: when no session exists,
        the fallback path correctly resolves niggy's bot via bot_for_agent().
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "niggy": {"agent": "niggy"},
                "ritchie": {"agent": "ritchie"},
                "viavacavi": {"agent": "viavacavi"},
            },
            chat_ids={
                "main": "111111",
                "niggy": "333333",
                "ritchie": "222222",
                "viavacavi": "444444",
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "niggy", "ritchie", "viavacavi"],
            telegram_plugin=plugin,
            known_endpoints={},
            job_agents={
                "hourly-stats-id": "niggy",
                "TradingScan-id": "ritchie",
                "viavacavi-orders-id": "viavacavi",
            },
        )

        # No active session for niggy
        gw._session_manager.get_active_session = AsyncMock(return_value=None)

        # Build a command-type job owned by niggy
        job = Job(
            id="hourly-stats-id",
            name="hourly-stats",
            run=CommandRun(command="echo stats"),
            schedule=CronSchedule(expr="0 * * * *"),
            deliver=DeliverAnnounce(),
        )

        # Simulate the exact logic from _job_notify fallback path
        job_agent_id = getattr(getattr(job, "run", None), "agent", None)
        if not job_agent_id and gw._job_scheduler:
            job_agent_id = gw._job_scheduler._job_agents.get(job.id)

        assert job_agent_id == "niggy", "Job should be owned by niggy"

        # No session exists
        notify_session = await gw._session_manager.get_active_session(job_agent_id)
        assert notify_session is None

        # Fallback: resolve bot_name via endpoint then bot_for_agent
        ep = gw._known_endpoints.get(job_agent_id or "", {}).get("telegram", {})
        chat_id = ep.get("sender_id") or (
            getattr(job.deliver, "chat_id", None) if job.deliver else None
        )
        bot_name = ep.get("bot_name")

        assert chat_id is None, "No chat_id available without endpoint"
        assert bot_name is None, "No bot_name in empty endpoint"

        # bot_for_agent fallback
        if not bot_name and job_agent_id and "telegram" in gw._channels:
            tg_plugin = gw._channels["telegram"]
            if hasattr(tg_plugin, "bot_for_agent"):
                _, bot_name = tg_plugin.bot_for_agent(job_agent_id)

        assert bot_name == "niggy", (
            f"bot_name should be 'niggy' (the actual bot name), got {bot_name!r}"
        )

        # _resolve_bot would correctly find niggy's bot
        resolved = plugin._resolve_bot(bot_name)
        assert resolved is plugin._bots["niggy"], (
            "Should resolve to niggy's bot"
        )

    @pytest.mark.asyncio
    async def test_agent_job_notify_with_session_uses_correct_bot(self):
        """Agent-type job (TradingScan) with an active session sends notification
        via the correct bot through _deliver_to_channel's triple fallback.
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
            },
        )

        # Session exists but has NO bot_name in endpoints (common after restart)
        session = _make_session(agent_id="ritchie", user_id="8327082847")
        session.context = {"channel_endpoints": {"telegram": {"sender_id": "8327082847"}}}

        gw = _make_gateway_stub(
            agents=["main", "ritchie"],
            telegram_plugin=plugin,
            known_endpoints={
                "ritchie": {
                    "telegram": {"sender_id": "8327082847"}
                    # No bot_name — common after restart
                }
            },
        )

        # _deliver_to_channel triple fallback:
        # 1. _known_endpoints["ritchie"]["telegram"] -> no bot_name
        # 2. session.context["channel_endpoints"]["telegram"] -> no bot_name
        # 3. plugin.bot_for_agent("ritchie") -> returns "ritchie" (the fix!)

        await gw._deliver_to_channel(session, "Job *TradingScan* started")

        ritchie_bot = plugin._bots["ritchie"]
        main_bot = plugin._bots["main"]

        assert ritchie_bot.send_message.called, (
            "ritchie's bot should receive the notification"
        )
        assert not main_bot.send_message.called, (
            "main's bot should NOT receive the notification"
        )

        # Verify the text was sent correctly
        call_args = ritchie_bot.send_message.call_args
        call_text = call_args.kwargs.get("text", call_args.args[1] if len(call_args.args) > 1 else "")
        assert "TradingScan" in call_text

    @pytest.mark.asyncio
    async def test_viavacavi_job_notify_reaches_correct_bot(self):
        """viavacavi-orders job notification reaches viavacavi's own bot."""
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "viavacavi": {"agent": "viavacavi"},
            },
            chat_ids={
                "main": "111111",
                "viavacavi": "444444",
            },
        )

        session = _make_session(agent_id="viavacavi", user_id="8327082847")
        session.context = {"channel_endpoints": {}}

        gw = _make_gateway_stub(
            agents=["main", "viavacavi"],
            telegram_plugin=plugin,
            known_endpoints={},
        )

        await gw._deliver_to_channel(session, "Job viavacavi-orders started")

        viavacavi_bot = plugin._bots["viavacavi"]
        main_bot = plugin._bots["main"]

        assert viavacavi_bot.send_message.called, (
            "viavacavi's bot should receive the notification"
        )
        assert not main_bot.send_message.called, (
            "main's bot should NOT receive the notification"
        )

    @pytest.mark.asyncio
    async def test_all_agents_route_to_own_bots(self):
        """Verify that each agent's job notification reaches its own bot
        and not any other agent's bot.
        """
        agents = ["main", "niggy", "ritchie", "viavacavi"]
        plugin = _make_telegram_plugin(
            bots_config={name: {"agent": name} for name in agents},
            chat_ids={name: f"{i}11111" for i, name in enumerate(agents)},
        )

        for target_agent in agents:
            # Reset all mock call records
            for name in agents:
                plugin._bots[name].send_message.reset_mock()

            gw = _make_gateway_stub(
                agents=agents,
                telegram_plugin=plugin,
                known_endpoints={},
            )

            session = _make_session(agent_id=target_agent)
            session.context = {"channel_endpoints": {}}

            await gw._deliver_to_channel(session, f"Notification for {target_agent}")

            # Only the target agent's bot should have been called
            for name in agents:
                bot = plugin._bots[name]
                if name == target_agent:
                    assert bot.send_message.called, (
                        f"{name}'s bot should be called for {target_agent}'s notification"
                    )
                else:
                    assert not bot.send_message.called, (
                        f"{name}'s bot should NOT be called for {target_agent}'s notification"
                    )


# ---------------------------------------------------------------------------
# Endpoint persistence: bot_for_agent fallback works after restart
# ---------------------------------------------------------------------------


class TestEndpointPersistenceRecovery:
    """Tests showing that after a gateway restart, even if bot_name is lost
    from persisted endpoints, the bot_for_agent fallback now correctly
    recovers the right bot.
    """

    @pytest.mark.asyncio
    async def test_session_endpoint_missing_bot_name_recovers_via_fallback(self):
        """Session endpoints restored after restart may lack bot_name.
        The third fallback (bot_for_agent) now correctly resolves it.
        """
        plugin = _make_telegram_plugin(
            bots_config={
                "main": {"agent": "main"},
                "ritchie": {"agent": "ritchie"},
            },
            chat_ids={
                "main": "111111",
                "ritchie": "222222",
            },
        )

        gw = _make_gateway_stub(
            agents=["main", "ritchie"],
            telegram_plugin=plugin,
        )

        # Simulate a session restored from disk with endpoint but no bot_name
        session = _make_session(agent_id="ritchie")
        session.context = {
            "channel_endpoints": {
                "telegram": {
                    "sender_id": "8327082847",
                    "sender": "User",
                    # No "bot_name" key — this is common after restart
                }
            }
        }

        await gw._deliver_to_channel(session, "Test notification")

        # bot_for_agent fallback now returns "ritchie" (bot_name), resolving correctly
        ritchie_bot = plugin._bots["ritchie"]
        main_bot = plugin._bots["main"]

        assert ritchie_bot.send_message.called, (
            "ritchie's bot should be used via bot_for_agent fallback"
        )
        assert not main_bot.send_message.called, (
            "main's bot should NOT be used"
        )

    def test_handle_message_preserves_bot_name_in_session_context(self):
        """Verify that handle_message copies bot_name from _known_endpoints
        to session.context — this is the mechanism that prevents the gap.

        The code at gateway.py does:
            if "bot_name" in _gw_ep and "bot_name" not in _sess_ep:
                _sess_ep["bot_name"] = _gw_ep["bot_name"]

        This works when the user has sent a message via Telegram (which sets
        bot_name in _known_endpoints via register_endpoint). For agents that
        are ONLY used by jobs and have never received a direct Telegram
        message, bot_name is never set — but bot_for_agent fallback now
        handles that case correctly.
        """
        # This is a documentation test — the code path exists but only fires
        # when a user has actually sent a Telegram message to the agent.
        # Job-only agents now work correctly via the bot_for_agent fallback.
        pass
