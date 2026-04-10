"""Tests for the easy-win slash commands:
  - /commands     — compact command listing
  - /models fallbacks — manage fallback chain at runtime
  - /debug        — session debug flags
  - /activation   — group activation mode
  - /elevated     — per-session exec approval mode
"""

import re
import pytest
from unittest.mock import MagicMock, AsyncMock


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_gateway(agent_id="main", session_id="sess-abc", fallbacks=None):
    from pyclopse.core.gateway import Gateway
    from pyclopse.channels.telegram_plugin import TelegramChannelConfig
    from pyclopse.channels.slack_plugin import SlackChannelConfig
    from pyclopse.config.schema import Config, AgentsConfig, ChannelsConfig

    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()
    gw._approval_system = MagicMock()
    gw._approval_system.always_approve = []

    tg = TelegramChannelConfig(allowed_users=[], denied_users=[])
    sl = SlackChannelConfig(allowed_users=[], denied_users=[])
    gw._config = Config(agents=AgentsConfig(), channels=ChannelsConfig(telegram=tg, slack=sl))

    from pyclopse.utils.time import now
    session = MagicMock()
    session.id = session_id
    session.agent_id = agent_id
    session.context = {}
    session.message_count = 3
    session.channel = "telegram"
    session.last_channel = "telegram"
    session.last_user_id = "u1"
    session.user_id = "u1"
    session.created_at = now()
    session.updated_at = now()

    agent = MagicMock()
    agent.config = MagicMock()
    agent.config.name = agent_id
    agent.config.model = "generic.MiniMax-M2.5"
    agent.config.fallbacks = list(fallbacks or [])
    agent._session_runners = {}

    am = MagicMock()
    am.get_agent = lambda aid: agent if aid == agent_id else None
    gw._agent_manager = am

    return gw, session, agent


def _ctx(gw=None, session=None, channel="telegram"):
    from pyclopse.core.commands import CommandContext
    if gw is None:
        gw, session, _ = _make_gateway()
    return CommandContext(gateway=gw, session=session, sender_id="u1", channel=channel)


def _registry(gw):
    from pyclopse.core.commands import CommandRegistry, register_builtin_commands
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    return r


async def _dispatch(cmd_text, gw=None, session=None):
    if gw is None:
        gw, session, _ = _make_gateway()
    from pyclopse.core.commands import CommandContext
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    return await _registry(gw).dispatch(cmd_text, ctx)


# ── /commands ─────────────────────────────────────────────────────────────────

async def test_commands_is_registered():
    result = await _dispatch("/commands")
    assert result is not None
    assert "Commands:" in result


async def test_commands_lists_all_commands():
    result = await _dispatch("/commands")
    # Should include well-known commands
    assert "/help" in result
    assert "/reset" in result
    assert "/status" in result


async def test_commands_is_compact_compared_to_help():
    gw, session, _ = _make_gateway()
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=session, sender_id="u1", channel="telegram")
    help_out = await r.dispatch("/help", ctx)
    commands_out = await r.dispatch("/commands", ctx)
    # Both list commands; /commands should not be longer than /help
    assert len(commands_out) <= len(help_out) + 200  # allow some slack


# ── /models fallbacks ─────────────────────────────────────────────────────────

async def test_models_fallbacks_list_empty():
    gw, session, _ = _make_gateway(fallbacks=[])
    result = await _dispatch("/models fallbacks", gw=gw, session=session)
    assert "No fallbacks" in result


async def test_models_fallbacks_list_shows_chain():
    gw, session, _ = _make_gateway(fallbacks=["model-b", "model-c"])
    result = await _dispatch("/models fallbacks", gw=gw, session=session)
    assert "model-b" in result
    assert "model-c" in result


async def test_models_fallbacks_add():
    gw, session, agent = _make_gateway(fallbacks=[])
    result = await _dispatch("/models fallbacks add generic.backup-model", gw=gw, session=session)
    assert "Added" in result
    assert "generic.backup-model" in agent.config.fallbacks


async def test_models_fallbacks_add_appends():
    gw, session, agent = _make_gateway(fallbacks=["model-a"])
    await _dispatch("/models fallbacks add model-b", gw=gw, session=session)
    assert agent.config.fallbacks == ["model-a", "model-b"]


async def test_models_fallbacks_remove():
    gw, session, agent = _make_gateway(fallbacks=["model-a", "model-b"])
    result = await _dispatch("/models fallbacks remove model-a", gw=gw, session=session)
    assert "Removed" in result
    assert "model-a" not in agent.config.fallbacks
    assert "model-b" in agent.config.fallbacks


async def test_models_fallbacks_remove_clears_index():
    gw, session, agent = _make_gateway(fallbacks=["model-a", "model-b"])
    session.context["_fallback_index"] = 1
    await _dispatch("/models fallbacks remove model-a", gw=gw, session=session)
    assert "_fallback_index" not in session.context


async def test_models_fallbacks_remove_not_found():
    gw, session, _ = _make_gateway(fallbacks=["model-a"])
    result = await _dispatch("/models fallbacks remove nonexistent", gw=gw, session=session)
    assert "not in" in result


async def test_models_fallbacks_clear():
    gw, session, agent = _make_gateway(fallbacks=["model-a", "model-b"])
    result = await _dispatch("/models fallbacks clear", gw=gw, session=session)
    assert "cleared" in result.lower()
    assert agent.config.fallbacks == []


async def test_models_fallbacks_clear_resets_index():
    gw, session, agent = _make_gateway(fallbacks=["model-a"])
    session.context["_fallback_index"] = 1
    await _dispatch("/models fallbacks clear", gw=gw, session=session)
    assert "_fallback_index" not in session.context


async def test_models_fallbacks_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, session, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/models fallbacks", ctx)
    assert "No active session" in result


async def test_models_shows_fallbacks_when_configured():
    gw, session, _ = _make_gateway(fallbacks=["backup-model"])
    result = await _dispatch("/models", gw=gw, session=session)
    assert "backup-model" in result


# ── /debug ────────────────────────────────────────────────────────────────────

async def test_debug_show_no_flags():
    result = await _dispatch("/debug")
    assert "No debug flags" in result


async def test_debug_set_string_value():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/debug set log_level verbose", gw=gw, session=session)
    assert "log_level" in result
    assert session.context["_debug"]["log_level"] == "verbose"


async def test_debug_set_bool_true():
    gw, session, _ = _make_gateway()
    await _dispatch("/debug set trace true", gw=gw, session=session)
    assert session.context["_debug"]["trace"] is True


async def test_debug_set_bool_false():
    gw, session, _ = _make_gateway()
    await _dispatch("/debug set trace false", gw=gw, session=session)
    assert session.context["_debug"]["trace"] is False


async def test_debug_set_int_value():
    gw, session, _ = _make_gateway()
    await _dispatch("/debug set retries 5", gw=gw, session=session)
    assert session.context["_debug"]["retries"] == 5


async def test_debug_show_flags():
    gw, session, _ = _make_gateway()
    session.context["_debug"] = {"trace": True, "level": "debug"}
    result = await _dispatch("/debug", gw=gw, session=session)
    assert "trace" in result
    assert "level" in result


async def test_debug_unset_existing():
    gw, session, _ = _make_gateway()
    session.context["_debug"] = {"trace": True}
    result = await _dispatch("/debug unset trace", gw=gw, session=session)
    assert "Unset" in result
    assert "trace" not in session.context.get("_debug", {})


async def test_debug_unset_missing():
    result = await _dispatch("/debug unset nonexistent")
    assert "not set" in result


async def test_debug_reset():
    gw, session, _ = _make_gateway()
    session.context["_debug"] = {"a": 1, "b": 2}
    result = await _dispatch("/debug reset", gw=gw, session=session)
    assert "cleared" in result.lower()
    assert "_debug" not in session.context


async def test_debug_set_missing_value_returns_usage():
    result = await _dispatch("/debug set key_only")
    assert "Usage" in result


async def test_debug_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/debug", ctx)
    assert "No active session" in result


# ── /activation ───────────────────────────────────────────────────────────────

async def test_activation_default_status():
    result = await _dispatch("/activation")
    assert "always" in result


async def test_activation_set_mention():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/activation mention", gw=gw, session=session)
    assert "mention" in result
    assert session.context["activation_mode"] == "mention"


async def test_activation_set_always():
    gw, session, _ = _make_gateway()
    session.context["activation_mode"] = "mention"
    result = await _dispatch("/activation always", gw=gw, session=session)
    assert "always" in result
    assert session.context["activation_mode"] == "always"


async def test_activation_invalid_mode():
    result = await _dispatch("/activation disco")
    assert "Usage" in result


async def test_activation_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/activation always", ctx)
    assert "No active session" in result


# ── /elevated ─────────────────────────────────────────────────────────────────

async def test_elevated_default_status():
    result = await _dispatch("/elevated")
    assert "off" in result


async def test_elevated_on_sets_context():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/elevated on", gw=gw, session=session)
    assert "on" in result
    assert session.context["elevated_mode"] == "on"


async def test_elevated_on_adds_catchall_to_approval():
    gw, session, _ = _make_gateway()
    await _dispatch("/elevated on", gw=gw, session=session)
    patterns = [p.pattern for p in gw._approval_system.always_approve]
    assert ".*" in patterns


async def test_elevated_on_idempotent():
    gw, session, _ = _make_gateway()
    await _dispatch("/elevated on", gw=gw, session=session)
    await _dispatch("/elevated on", gw=gw, session=session)
    catchalls = sum(1 for p in gw._approval_system.always_approve if p.pattern == ".*")
    assert catchalls == 1


async def test_elevated_off_removes_catchall():
    gw, session, _ = _make_gateway()
    await _dispatch("/elevated on", gw=gw, session=session)
    result = await _dispatch("/elevated off", gw=gw, session=session)
    assert "off" in result
    patterns = [p.pattern for p in gw._approval_system.always_approve]
    assert ".*" not in patterns


async def test_elevated_off_clears_context():
    gw, session, _ = _make_gateway()
    await _dispatch("/elevated on", gw=gw, session=session)
    await _dispatch("/elevated off", gw=gw, session=session)
    assert session.context["elevated_mode"] == "off"


async def test_elevated_ask():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/elevated ask", gw=gw, session=session)
    assert "ask" in result
    assert session.context["elevated_mode"] == "ask"


async def test_elevated_full():
    gw, session, _ = _make_gateway()
    result = await _dispatch("/elevated full", gw=gw, session=session)
    assert "full" in result
    assert session.context["elevated_mode"] == "full"


async def test_elevated_invalid_mode():
    result = await _dispatch("/elevated superadmin")
    assert "Usage" in result


async def test_elevated_no_session():
    from pyclopse.core.commands import CommandContext, CommandRegistry, register_builtin_commands
    gw, _, _ = _make_gateway()
    r = CommandRegistry()
    register_builtin_commands(r, gw)
    ctx = CommandContext(gateway=gw, session=None, sender_id="u1", channel="telegram")
    result = await r.dispatch("/elevated on", ctx)
    assert "No active session" in result
