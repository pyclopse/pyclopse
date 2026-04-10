"""Tests for new slash commands and ACP pass-through handlers.

Covers:
  - /mcp, /cards, /card, /agent — FA ACP pass-through
  - /bash — shell command → agent context
  - /allowlist — runtime allowlist management (telegram + slack)
  - /reasoning — toggle show_thinking on session runner
  - /session — show session info and set overrides
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_runner(initialized=True, session_id="sess-abc"):
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner.agent_name = "main"
    runner.session_id = session_id
    runner._app = MagicMock() if initialized else None
    runner._fa_app = MagicMock() if initialized else None
    runner._slash_handler = None
    runner.show_thinking = False
    runner.acp_execute = AsyncMock(return_value="acp response")
    return runner


def _make_gateway(agent_id="main", session_id="sess-abc", runner=None, channel="telegram"):
    from pyclopse.core.gateway import Gateway
    from pyclopse.channels.telegram_plugin import TelegramChannelConfig
    from pyclopse.channels.slack_plugin import SlackChannelConfig
    from pyclopse.config.schema import (
        Config, AgentsConfig, ChannelsConfig,
    )

    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()

    tg = TelegramChannelConfig(allowed_users=[], denied_users=[])
    sl = SlackChannelConfig(allowed_users=[], denied_users=[])
    channels = ChannelsConfig(telegram=tg, slack=sl)
    gw._config = Config(agents=AgentsConfig(), channels=channels)

    session = MagicMock()
    session.id = session_id
    session.agent_id = agent_id
    session.context = {}
    session.message_count = 5
    session.channel = channel
    session.last_channel = channel
    session.last_user_id = "u1"
    session.user_id = "u1"
    from pyclopse.utils.time import now
    session.created_at = now()
    session.updated_at = now()

    agent = MagicMock()
    agent.config = MagicMock()
    agent.config.model = "sonnet"
    agent._session_runners = {session_id: runner} if runner else {}

    am = MagicMock()
    am.get_agent = lambda aid: agent if aid == agent_id else None
    gw._agent_manager = am

    return gw, session, agent


def _make_ctx(channel="telegram", runner=None):
    from pyclopse.core.commands import CommandContext
    gw, session, agent = _make_gateway(channel=channel, runner=runner)
    return CommandContext(gateway=gw, session=session, sender_id="u1", channel=channel)


def _registry(gw):
    from pyclopse.core.commands import CommandRegistry, register_builtin_commands
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    return r


# ── ACP pass-through: /mcp, /cards, /card, /agent ─────────────────────────────

@pytest.mark.asyncio
async def test_mcp_routes_to_acp():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    ctx = _make_ctx.__wrapped__ if hasattr(_make_ctx, "__wrapped__") else None
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/mcp list", ctx)
    runner.acp_execute.assert_called_once_with("mcp", "list")


@pytest.mark.asyncio
async def test_cards_routes_to_acp():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/cards", ctx)
    runner.acp_execute.assert_called_once_with("cards", "")


@pytest.mark.asyncio
async def test_card_routes_to_acp():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/card myagent", ctx)
    runner.acp_execute.assert_called_once_with("card", "myagent")


@pytest.mark.asyncio
async def test_agent_routes_to_acp():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/agent info", ctx)
    runner.acp_execute.assert_called_once_with("agent", "info")


@pytest.mark.asyncio
async def test_acp_passthrough_no_runner_returns_message():
    gw, session, _ = _make_gateway(runner=None)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/mcp list", ctx)
    assert result is not None
    assert "session" in result.lower() or "message" in result.lower()


# ── /bash ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_no_args_returns_usage():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/bash", ctx)
    assert "usage" in result.lower() or "bash" in result.lower()


@pytest.mark.asyncio
async def test_bash_runs_command_and_sends_to_agent():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext

    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    agent = gw._agent_manager.get_agent("main")
    mock_result = MagicMock()
    mock_result.content = "agent processed output"
    agent.handle_message = AsyncMock(return_value=mock_result)

    result = await _registry(gw).dispatch("/bash echo hello", ctx)
    assert result == "agent processed output"

    # Verify handle_message was called with output containing the command
    call_args = agent.handle_message.call_args
    msg = call_args[0][0]
    assert "echo hello" in msg.content
    assert "hello" in msg.content


@pytest.mark.asyncio
async def test_bash_timeout_returns_error():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    import asyncio

    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")

    with patch("asyncio.create_subprocess_shell", side_effect=asyncio.TimeoutError):
        result = await _registry(gw).dispatch("/bash sleep 999", ctx)
        assert "timeout" in result.lower() or "error" in result.lower()


@pytest.mark.asyncio
async def test_bash_no_session_returns_raw_output():
    """Without a session, /bash still runs and returns the raw output."""
    from pyclopse.core.gateway import Gateway
    from pyclopse.config.schema import Config, AgentsConfig
    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()
    gw._config = Config(agents=AgentsConfig())
    gw._agent_manager = None
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    registry = CommandRegistry()
    register_builtin_commands(registry, gw)
    result = await registry.dispatch("/bash echo rawtest", ctx)
    assert "rawtest" in result


# ── /allowlist ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allowlist_list_empty():
    gw, session, _ = _make_gateway(channel="telegram")
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/allowlist list", ctx)
    assert "empty" in result.lower() or "all" in result.lower()


@pytest.mark.asyncio
async def test_allowlist_add_telegram():
    gw, session, _ = _make_gateway(channel="telegram")
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/allowlist add 12345", ctx)
    assert "12345" in result
    assert 12345 in gw._config.channels.telegram.allowed_users


@pytest.mark.asyncio
async def test_allowlist_add_duplicate_is_idempotent():
    gw, session, _ = _make_gateway(channel="telegram")
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/allowlist add 12345", ctx)
    await _registry(gw).dispatch("/allowlist add 12345", ctx)
    assert gw._config.channels.telegram.allowed_users.count(12345) == 1


@pytest.mark.asyncio
async def test_allowlist_remove_telegram():
    gw, session, _ = _make_gateway(channel="telegram")
    gw._config.channels.telegram.allowed_users.append(12345)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/allowlist remove 12345", ctx)
    assert "12345" in result
    assert 12345 not in gw._config.channels.telegram.allowed_users


@pytest.mark.asyncio
async def test_allowlist_list_shows_users():
    gw, session, _ = _make_gateway(channel="telegram")
    gw._config.channels.telegram.allowed_users.extend([111, 222])
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/allowlist", ctx)
    assert "111" in result
    assert "222" in result


@pytest.mark.asyncio
async def test_allowlist_slack_add():
    gw, session, _ = _make_gateway(channel="slack")
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="slack")
    result = await _registry(gw).dispatch("/allowlist add U123ABC", ctx)
    assert "U123ABC" in result
    assert "U123ABC" in gw._config.channels.slack.allowed_users


@pytest.mark.asyncio
async def test_allowlist_unsupported_channel():
    gw, session, _ = _make_gateway(channel="discord")
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="discord")
    result = await _registry(gw).dispatch("/allowlist add 123", ctx)
    assert "not supported" in result.lower()


# ── /reasoning ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reasoning_status_default_off():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/reasoning status", ctx)
    assert "off" in result.lower()


@pytest.mark.asyncio
async def test_reasoning_on_sets_context_and_runner():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/reasoning on", ctx)
    assert session.context.get("show_thinking") is True
    assert runner.show_thinking is True
    assert "on" in result.lower()


@pytest.mark.asyncio
async def test_reasoning_stream_same_as_on():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/reasoning stream", ctx)
    assert session.context.get("show_thinking") is True


@pytest.mark.asyncio
async def test_reasoning_off_clears():
    runner = _make_runner()
    runner.show_thinking = True
    gw, session, _ = _make_gateway(runner=runner)
    session.context["show_thinking"] = True
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    await _registry(gw).dispatch("/reasoning off", ctx)
    assert session.context.get("show_thinking") is False
    assert runner.show_thinking is False


@pytest.mark.asyncio
async def test_reasoning_no_session():
    gw, _, _ = _make_gateway()
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/reasoning on", ctx)
    assert "session" in result.lower()


# ── /session ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_shows_info():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/session", ctx)
    assert "sess-abc" in result
    assert "main" in result


@pytest.mark.asyncio
async def test_session_timeout_sets_context():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/session timeout 60", ctx)
    assert session.context.get("idle_timeout_minutes") == 60
    assert "60" in result


@pytest.mark.asyncio
async def test_session_window_sets_context():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/session window 20", ctx)
    assert session.context.get("window_size") == 20
    assert "20" in result


@pytest.mark.asyncio
async def test_session_no_session():
    gw, _, _ = _make_gateway()
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/session", ctx)
    assert "session" in result.lower()


@pytest.mark.asyncio
async def test_session_invalid_timeout():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    result = await _registry(gw).dispatch("/session timeout notanumber", ctx)
    assert "usage" in result.lower() or "timeout" in result.lower()
