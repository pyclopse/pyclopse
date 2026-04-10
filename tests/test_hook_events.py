"""Tests for the three new hook events:
  - message:preprocessed — fires after checks, before agent dispatch
  - agent:bootstrap       — fires when a new session runner is created
  - message:transcribed   — constant exists; not yet fired (no voice input)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway_stub(agent_id="main", session_id="sess-x"):
    """Build a minimal Gateway-like stub with just enough wiring for handle_message."""
    from pyclopse.core.gateway import Gateway
    from pyclopse.channels.telegram_plugin import TelegramChannelConfig
    from pyclopse.config.schema import (
        Config, AgentsConfig, ChannelsConfig, SlackConfig,
    )
    from pyclopse.core.session import Session

    gw = Gateway.__new__(Gateway)
    gw._logger = MagicMock()
    gw._hook_registry = None
    gw._audit_logger = None
    gw._approval_system = MagicMock()
    gw._approval_system.always_approve = []
    gw._thread_bindings = {}
    gw._usage = {
        "messages_total": 0,
        "messages_by_agent": {},
        "messages_by_channel": {},
    }

    tg = TelegramChannelConfig(allowed_users=[], denied_users=[])
    sl = SlackConfig(allowed_users=[], denied_users=[])
    gw._config = Config(agents=AgentsConfig(), channels=ChannelsConfig(telegram=tg, slack=sl))

    # Session
    session = Session(id=session_id, agent_id=agent_id, channel="telegram", user_id="u1")

    # Agent stub
    agent = MagicMock()
    agent.id = agent_id
    agent.config = MagicMock()
    agent.config_dir = "~/.pyclopse"
    agent._session_runners = {}
    agent.handle_message = AsyncMock(return_value=MagicMock(content="ok"))

    am = MagicMock()
    am.get_agent = lambda aid: agent if aid == agent_id else None
    am.agents = {agent_id: agent}
    gw._agent_manager = am

    return gw, session, agent


# ---------------------------------------------------------------------------
# message:preprocessed
# ---------------------------------------------------------------------------

async def test_message_preprocessed_fires_before_agent():
    """message:preprocessed fires after activation_mode check, before agent.handle_message."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub()

    fired_events = []

    async def fake_fire(event, ctx):
        fired_events.append((event, ctx))

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        await gw.handle_message(
            channel="telegram",
            sender="Alice",
            sender_id="u1",
            content="hello world",
            message_id="m1",
            agent_id="main",
        )

    event_names = [e for e, _ in fired_events]
    assert HookEvent.MESSAGE_PREPROCESSED in event_names
    assert HookEvent.AGENT_RESPONSE in event_names

    # preprocessed fires BEFORE agent_response
    pre_idx = event_names.index(HookEvent.MESSAGE_PREPROCESSED)
    resp_idx = event_names.index(HookEvent.AGENT_RESPONSE)
    assert pre_idx < resp_idx


async def test_message_preprocessed_payload():
    """message:preprocessed payload has expected fields."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub(session_id="sess-payload")

    preprocessed_ctx = {}

    async def fake_fire(event, ctx):
        if event == HookEvent.MESSAGE_PREPROCESSED:
            preprocessed_ctx.update(ctx)

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        await gw.handle_message(
            channel="telegram",
            sender="Alice",
            sender_id="u1",
            content="test content",
            message_id="m2",
            agent_id="main",
        )

    assert preprocessed_ctx["body_for_agent"] == "test content"
    assert preprocessed_ctx["channel"] == "telegram"
    assert preprocessed_ctx["sender_id"] == "u1"
    assert preprocessed_ctx["session_id"] == "sess-payload"
    assert preprocessed_ctx["agent_id"] == "main"
    assert "transcript" in preprocessed_ctx


async def test_message_preprocessed_not_fired_on_activation_mode_skip():
    """message:preprocessed must NOT fire when activation_mode=mention skips the message."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub()
    session.context["activation_mode"] = "mention"  # requires agent name in content

    fired_events = []

    async def fake_fire(event, ctx):
        fired_events.append(event)

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        result = await gw.handle_message(
            channel="telegram",
            sender="Alice",
            sender_id="u1",
            content="hello without agent mention",
            message_id="m3",
            agent_id="main",
        )

    assert result is None  # message was skipped
    assert HookEvent.MESSAGE_PREPROCESSED not in fired_events


# ---------------------------------------------------------------------------
# agent:bootstrap
# ---------------------------------------------------------------------------

async def test_agent_bootstrap_fires_on_new_runner(tmp_path):
    """agent:bootstrap fires when session_id is not yet in agent._session_runners."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub(session_id="sess-new")
    agent._session_runners = {}  # no existing runner → new
    agent.config_dir = str(tmp_path)

    # Create a couple of bootstrap files so the payload is non-empty
    agents_dir = tmp_path / "agents" / "main"
    agents_dir.mkdir(parents=True)
    (agents_dir / "AGENTS.md").write_text("workspace instructions")
    (agents_dir / "SOUL.md").write_text("personality")

    bootstrap_ctx = {}

    async def fake_fire(event, ctx):
        if event == HookEvent.AGENT_BOOTSTRAP:
            bootstrap_ctx.update(ctx)

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        await gw.handle_message(
            channel="telegram",
            sender="Alice",
            sender_id="u1",
            content="hi",
            message_id="m4",
            agent_id="main",
        )

    assert bootstrap_ctx["agent_id"] == "main"
    assert bootstrap_ctx["session_id"] == "sess-new"
    assert "workspace_dir" in bootstrap_ctx
    assert isinstance(bootstrap_ctx["bootstrap_files"], list)
    assert any("AGENTS.md" in f for f in bootstrap_ctx["bootstrap_files"])
    assert any("SOUL.md" in f for f in bootstrap_ctx["bootstrap_files"])


async def test_agent_bootstrap_not_fired_on_existing_runner():
    """agent:bootstrap must NOT fire when the session runner already exists."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub(session_id="sess-old")
    agent._session_runners = {"sess-old": MagicMock()}  # runner already present

    fired_events = []

    async def fake_fire(event, ctx):
        fired_events.append(event)

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        await gw.handle_message(
            channel="telegram",
            sender="Alice",
            sender_id="u1",
            content="second message",
            message_id="m5",
            agent_id="main",
        )

    assert HookEvent.AGENT_BOOTSTRAP not in fired_events


async def test_agent_bootstrap_fires_once_across_multiple_messages(tmp_path):
    """Bootstrap event fires only on the first message, not on subsequent ones."""
    from pyclopse.hooks.events import HookEvent

    gw, session, agent = _make_gateway_stub(session_id="sess-multi")
    agent._session_runners = {}
    agent.config_dir = str(tmp_path)
    (tmp_path / "agents" / "main").mkdir(parents=True)

    bootstrap_count = 0

    async def fake_fire(event, ctx):
        nonlocal bootstrap_count
        if event == HookEvent.AGENT_BOOTSTRAP:
            bootstrap_count += 1
            # Simulate runner being created after first message
            agent._session_runners["sess-multi"] = MagicMock()

    gw._fire = fake_fire

    with (
        patch("pyclopse.core.gateway._snapshot_ctx_tokens"),
        patch.object(gw, "_get_active_session", AsyncMock(return_value=session)),
    ):
        for _ in range(3):
            await gw.handle_message(
                channel="telegram",
                sender="Alice",
                sender_id="u1",
                content="ping",
                message_id="mx",
                agent_id="main",
            )

    assert bootstrap_count == 1


# ---------------------------------------------------------------------------
# HookEvent constants
# ---------------------------------------------------------------------------

def test_hook_event_constants_exist():
    """All three new event constants must be importable from HookEvent."""
    from pyclopse.hooks.events import HookEvent

    assert HookEvent.AGENT_BOOTSTRAP == "agent:bootstrap"
    assert HookEvent.MESSAGE_PREPROCESSED == "message:preprocessed"
    assert HookEvent.MESSAGE_TRANSCRIBED == "message:transcribed"


def test_message_transcribed_not_interceptable():
    """message:transcribed is a notification event (not in INTERCEPTABLE set)."""
    from pyclopse.hooks.events import HookEvent

    assert HookEvent.MESSAGE_TRANSCRIBED not in HookEvent.INTERCEPTABLE
    assert HookEvent.MESSAGE_PREPROCESSED not in HookEvent.INTERCEPTABLE
    assert HookEvent.AGENT_BOOTSTRAP not in HookEvent.INTERCEPTABLE
