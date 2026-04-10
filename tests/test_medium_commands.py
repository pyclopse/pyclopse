"""Tests for the medium-effort slash commands:
  - /acp        — FA ACP pass-through
  - /exec       — per-session exec settings
  - /send       — outbound send policy
  - /focus      — bind thread/topic to agent
  - /unfocus    — remove thread binding
  - /agents     — list thread bindings
  - /status     — context token usage section
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_runner(session_id="sess-abc"):
    from pyclopse.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner.agent_name = "main"
    runner.session_id = session_id
    runner._app = MagicMock()
    runner._fa_app = MagicMock()
    runner._slash_handler = None
    runner.show_thinking = False
    runner.acp_execute = AsyncMock(return_value="acp response")
    return runner


def _make_gateway(agent_id="main", session_id="sess-abc", runner=None, thread_bindings=None):
    from pyclopse.core.gateway import Gateway
    from pyclopse.channels.telegram_plugin import TelegramChannelConfig
    from pyclopse.config.schema import Config, AgentsConfig, ChannelsConfig, SlackConfig

    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()
    gw._approval_system = MagicMock()
    gw._approval_system.always_approve = []
    gw._thread_bindings = dict(thread_bindings or {})

    tg = TelegramChannelConfig(allowed_users=[], denied_users=[])
    sl = SlackConfig(allowed_users=[], denied_users=[])
    gw._config = Config(agents=AgentsConfig(), channels=ChannelsConfig(telegram=tg, slack=sl))

    from pyclopse.utils.time import now
    session = MagicMock()
    session.id = session_id
    session.agent_id = agent_id
    session.context = {}
    session.history_dir = None

    agent = MagicMock()
    agent.config = MagicMock()
    agent.config.name = agent_id
    agent.config.model = "generic.model"
    agent.config.context_window = None
    agent._session_runners = {session_id: runner} if runner else {}

    am = MagicMock()
    am.get_agent = lambda aid: agent if aid == agent_id else None
    am.agents = {agent_id: agent}
    gw._agent_manager = am

    return gw, session, agent


def _ctx(gw, session, channel="telegram", thread_id=None):
    from pyclopse.core.commands import CommandContext
    return CommandContext(
        gateway=gw, session=session,
        sender_id="u1", channel=channel, thread_id=thread_id,
    )


def _registry(gw):
    from pyclopse.core.commands import CommandRegistry, register_builtin_commands
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    return r


async def _dispatch(cmd_text, gw, session, channel="telegram", thread_id=None):
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(
        gateway=gw, session=session,
        sender_id="u1", channel=channel, thread_id=thread_id,
    )
    return await _registry(gw).dispatch(cmd_text, ctx)


# ── /acp ─────────────────────────────────────────────────────────────────────

async def test_acp_routes_to_runner():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    result = await _dispatch("/acp sessions", gw, session)
    runner.acp_execute.assert_called_once_with("acp", "sessions")


async def test_acp_no_runner_returns_message():
    gw, session, _ = _make_gateway(runner=None)
    result = await _dispatch("/acp status", gw, session)
    assert "send a message first" in result.lower() or "no active" in result.lower()


async def test_acp_no_args_routes_empty():
    runner = _make_runner()
    gw, session, _ = _make_gateway(runner=runner)
    await _dispatch("/acp", gw, session)
    runner.acp_execute.assert_called_once_with("acp", "")


# ── /exec ─────────────────────────────────────────────────────────────────────

async def test_exec_show_defaults():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/exec", gw, session)
    assert "gateway" in result
    assert "normal" in result
    assert "inherit" in result


async def test_exec_set_host():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/exec host sandbox", gw, session)
    assert "sandbox" in result
    assert session.context["exec_host"] == "sandbox"


async def test_exec_set_host_invalid():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/exec host cloud", gw, session)
    assert "Usage" in result


async def test_exec_set_level():
    gw, session, _ = _make_gateway()
    await _dispatch("/exec level permissive", gw, session)
    assert session.context["exec_level"] == "permissive"


async def test_exec_set_level_invalid():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/exec level extreme", gw, session)
    assert "Usage" in result


async def test_exec_set_ask():
    gw, session, _ = _make_gateway()
    await _dispatch("/exec ask always", gw, session)
    assert session.context["exec_ask"] == "always"


async def test_exec_set_ask_invalid():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/exec ask maybe", gw, session)
    assert "Usage" in result


async def test_exec_reset():
    gw, session, _ = _make_gateway()
    session.context["exec_host"] = "sandbox"
    session.context["exec_level"] = "strict"
    result = await _dispatch("/exec reset", gw, session)
    assert "reset" in result.lower()
    assert "exec_host" not in session.context
    assert "exec_level" not in session.context


async def test_exec_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/exec", ctx)
    assert "No active session" in result


# ── /send ─────────────────────────────────────────────────────────────────────

async def test_send_default_status():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/send", gw, session)
    assert "on" in result


async def test_send_off():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/send off", gw, session)
    assert "off" in result
    assert session.context["send_policy"] == "off"


async def test_send_on():
    gw, session, _ = _make_gateway()
    session.context["send_policy"] = "off"
    result = await _dispatch("/send on", gw, session)
    assert "on" in result
    assert session.context["send_policy"] == "on"


async def test_send_inherit():
    gw, session, _ = _make_gateway()
    await _dispatch("/send inherit", gw, session)
    assert session.context["send_policy"] == "inherit"


async def test_send_invalid():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/send maybe", gw, session)
    assert "Usage" in result


async def test_send_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/send off", ctx)
    assert "No active session" in result


# ── /focus ────────────────────────────────────────────────────────────────────

async def test_focus_binds_current_agent():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/focus", gw, session, thread_id="123")
    assert "main" in result
    assert gw._thread_bindings.get("telegram:123") == "main"


async def test_focus_binds_specific_agent():
    gw, session, agent = _make_gateway()
    # make a second agent
    agent2 = MagicMock()
    agent2.config.name = "other"
    gw._agent_manager.agents["other"] = agent2
    gw._agent_manager.get_agent = lambda aid: {
        "main": agent, "other": agent2
    }.get(aid)
    result = await _dispatch("/focus other", gw, session, thread_id="456")
    assert "other" in result
    assert gw._thread_bindings.get("telegram:456") == "other"


async def test_focus_unknown_agent():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/focus nonexistent", gw, session, thread_id="123")
    assert "not found" in result.lower()


async def test_focus_no_thread_id():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/focus", gw, session, thread_id=None)
    assert "No thread ID" in result or "thread" in result.lower()


async def test_focus_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram", thread_id="123")
    result = await r.dispatch("/focus", ctx)
    assert "No active session" in result


# ── /unfocus ──────────────────────────────────────────────────────────────────

async def test_unfocus_removes_binding():
    gw, session, _ = _make_gateway(thread_bindings={"telegram:789": "main"})
    result = await _dispatch("/unfocus", gw, session, thread_id="789")
    assert "unbound" in result.lower() or "removed" in result.lower()
    assert "telegram:789" not in gw._thread_bindings


async def test_unfocus_no_binding():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/unfocus", gw, session, thread_id="999")
    assert "no binding" in result.lower()


async def test_unfocus_no_thread_id():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/unfocus", gw, session, thread_id=None)
    assert "No thread ID" in result or "thread" in result.lower()


# ── /agents ───────────────────────────────────────────────────────────────────

async def test_agents_empty():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/agents", gw, session, channel="telegram")
    assert "No thread bindings" in result or "no" in result.lower()


async def test_agents_lists_bindings():
    gw, session, _ = _make_gateway(thread_bindings={
        "telegram:100": "main",
        "telegram:200": "ritchie",
        "slack:T123": "other",  # different channel, should not appear
    })
    result = await _dispatch("/agents", gw, session, channel="telegram")
    assert "100" in result
    assert "200" in result
    assert "T123" not in result  # slack binding excluded


async def test_agents_marks_current_thread():
    gw, session, _ = _make_gateway(thread_bindings={"telegram:42": "main"})
    result = await _dispatch("/agents", gw, session, channel="telegram", thread_id="42")
    assert "current" in result


# ── /status context usage ─────────────────────────────────────────────────────

def _mock_get_status(gw):
    """Give the stub gateway a working get_status()."""
    gw.get_status = MagicMock(return_value={
        "is_running": True,
        "config_version": "1.0",
        "agents": {"total_agents": 1, "running_agents": 1},
        "sessions": {"active_sessions": 1, "total_sessions": 5},
        "jobs": {"total": 0, "running": 0},
    })


def _make_runner_with_usage(session_id, ctx_tokens, ctx_window=None):
    """Runner whose FA agent has a live usage_accumulator."""
    runner = _make_runner(session_id)
    accumulator = MagicMock()
    accumulator.current_context_tokens = ctx_tokens
    accumulator.context_window_size = ctx_window
    fa_agent = MagicMock()
    fa_agent.usage_accumulator = accumulator
    runner._app = MagicMock()
    runner._app._agent = MagicMock(return_value=fa_agent)
    return runner


async def test_status_no_runner_no_context_line():
    """No runner AND no snapshot → no Context line in /status output."""
    gw, session, _ = _make_gateway(runner=None)
    _mock_get_status(gw)
    result = await _dispatch("/status", gw, session)
    assert "pyclopse" in result.lower() or "running" in result.lower()
    assert "Context:" not in result


async def test_status_from_session_context_snapshot():
    """No live runner but session.context has _ctx_tokens → Context line shown."""
    gw, session, agent = _make_gateway(runner=None)
    _mock_get_status(gw)
    session.context["_ctx_tokens"] = 15000
    agent.config.context_window = 200000
    result = await _dispatch("/status", gw, session)
    assert "Context:" in result
    assert "15,000" in result
    assert "200,000" in result
    assert "%" in result


async def test_status_with_usage_no_limit():
    """Runner has usage_accumulator but no context_window → 'no limit' line."""
    runner = _make_runner_with_usage("sess-abc", ctx_tokens=8500, ctx_window=None)
    gw, session, agent = _make_gateway(runner=runner)
    _mock_get_status(gw)
    agent.config.context_window = None
    result = await _dispatch("/status", gw, session)
    assert "Context:" in result
    assert "8,500" in result
    assert "no limit" in result.lower()


async def test_status_with_usage_and_limit():
    """Runner has usage_accumulator + context_window → progress bar shown."""
    runner = _make_runner_with_usage("sess-abc", ctx_tokens=12000, ctx_window=None)
    gw, session, agent = _make_gateway(runner=runner)
    _mock_get_status(gw)
    agent.config.context_window = 200000
    result = await _dispatch("/status", gw, session)
    assert "Context:" in result
    assert "12,000" in result
    assert "200,000" in result
    assert "%" in result
    assert "█" in result or "░" in result


async def test_status_accumulator_falls_back_to_fa_context_window():
    """When agent.config.context_window is None, FA's accumulator.context_window_size is used."""
    runner = _make_runner_with_usage("sess-abc", ctx_tokens=5000, ctx_window=128000)
    gw, session, agent = _make_gateway(runner=runner)
    _mock_get_status(gw)
    agent.config.context_window = None
    result = await _dispatch("/status", gw, session)
    assert "128,000" in result
    assert "%" in result


async def test_status_zero_tokens_no_context_line():
    """Runner with 0 ctx_tokens → no Context line."""
    runner = _make_runner_with_usage("sess-abc", ctx_tokens=0)
    gw, session, _ = _make_gateway(runner=runner)
    _mock_get_status(gw)
    result = await _dispatch("/status", gw, session)
    assert "Context:" not in result
