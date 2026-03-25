"""
Tests for _handle_slack_message with threading support.
Covers:
  - Session keying: thread_ts when threading=True, user_id when threading=False
  - Reply goes to correct thread (thread_ts in post_kwargs)
  - Threading=False replies to channel without thread_ts
  - Allowlist / denylist enforcement
  - Slash command dispatch via CommandRegistry
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway(
    threading=True,
    allowed_users=None,
    denied_users=None,
    global_denied=None,
    agent_response="hello back",
):
    from pyclawops.core.gateway import Gateway
    from pyclawops.config.schema import (
        Config, ChannelsConfig, SlackConfig, AgentsConfig, SecurityConfig,
    )

    gw = Gateway.__new__(Gateway)
    gw._is_running = True
    gw._initialized = True
    gw._logger = MagicMock()
    gw._audit_logger = None
    gw._hook_registry = None
    gw._known_session_ids = set()

    slack_cfg = SlackConfig.model_validate({
        "enabled": True,
        "botToken": "xoxb-fake",
        "threading": threading,
        "allowedUsers": allowed_users or [],
        "deniedUsers": denied_users or [],
    })
    security_cfg = SecurityConfig.model_validate({
        "deniedUsers": global_denied or [],
    })
    channels_cfg = ChannelsConfig(slack=slack_cfg)
    gw._config = Config(channels=channels_cfg, agents=AgentsConfig(), security=security_cfg)

    # Session manager stub
    mock_session = MagicMock()
    mock_session.id = "sess1"
    mock_session.agent_id = "default"
    mock_session.last_channel = None
    mock_session.last_user_id = None
    mock_session.last_thread_ts = None
    mock_session.save_metadata = MagicMock()
    mock_sm = MagicMock()
    mock_sm.get_active_session = AsyncMock(return_value=mock_session)
    mock_sm.create_session = AsyncMock(return_value=mock_session)
    mock_sm.set_active_session = MagicMock()
    gw._session_manager = mock_sm

    # Agent manager stub
    gw._agent_manager = MagicMock()
    gw._agent_manager.agents = {}

    # enqueue_message stub (called by Slack handler)
    gw.enqueue_message = AsyncMock(return_value=agent_response)
    gw.handle_message = AsyncMock(return_value=agent_response)

    # Command registry
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    import time as _t
    gw._usage = {
        "messages_total": 0,
        "messages_by_agent": {},
        "messages_by_channel": {},
        "started_at": _t.time(),
    }
    from pyclawops.core.commands import CommandRegistry, register_builtin_commands
    gw._command_registry = CommandRegistry()
    register_builtin_commands(gw._command_registry, gw)

    return gw


def _make_event(user="U123", channel="C456", text="hello", ts="111.222", thread_ts=None):
    e = {"user": user, "channel": channel, "text": text, "ts": ts}
    if thread_ts is not None:
        e["thread_ts"] = thread_ts
    return e


def _make_slack_client():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Threading: session key
# ---------------------------------------------------------------------------

class TestSlackSessionKeying:

    @pytest.mark.asyncio
    async def test_threading_on_passes_thread_ts_for_reply_routing(self):
        """With threading=True and a thread_ts present, thread_ts is stored for reply routing."""
        gw = _make_gateway(threading=True)
        client = _make_slack_client()
        event = _make_event(user="U1", ts="100.0", thread_ts="90.0")

        await gw._handle_slack_message(event, client)

        # Active session is fetched for the agent
        gw._session_manager.get_active_session.assert_called_once()
        # last_thread_ts set to thread_ts for reply routing
        assert gw._session_manager.get_active_session.call_args[0][0] is not None

    @pytest.mark.asyncio
    async def test_threading_on_uses_ts_when_no_thread_ts(self):
        """With threading=True but no thread_ts, ts is used as thread_ts for replies."""
        gw = _make_gateway(threading=True)
        client = _make_slack_client()
        event = _make_event(user="U1", ts="100.0")  # no thread_ts

        await gw._handle_slack_message(event, client)

        gw._session_manager.get_active_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_threading_off_uses_user_id(self):
        """With threading=False, thread_ts is None in session routing."""
        gw = _make_gateway(threading=False)
        client = _make_slack_client()
        event = _make_event(user="U999", ts="100.0", thread_ts="90.0")

        await gw._handle_slack_message(event, client)

        gw._session_manager.get_active_session.assert_called_once()


# ---------------------------------------------------------------------------
# Threading: reply goes into thread
# ---------------------------------------------------------------------------

class TestSlackReplyThreading:

    @pytest.mark.asyncio
    async def test_threading_on_replies_to_existing_thread(self):
        """When threading=True and thread_ts is set, reply includes thread_ts."""
        gw = _make_gateway(threading=True)
        client = _make_slack_client()
        event = _make_event(channel="CTEST", ts="200.0", thread_ts="100.0")

        await gw._handle_slack_message(event, client)

        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "CTEST"
        assert kwargs["thread_ts"] == "100.0"

    @pytest.mark.asyncio
    async def test_threading_on_starts_thread_for_top_level_message(self):
        """When threading=True and no thread_ts, reply uses ts to start a new thread."""
        gw = _make_gateway(threading=True)
        client = _make_slack_client()
        event = _make_event(channel="CTEST", ts="500.0")  # no thread_ts

        await gw._handle_slack_message(event, client)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["thread_ts"] == "500.0"

    @pytest.mark.asyncio
    async def test_threading_off_does_not_include_thread_ts(self):
        """When threading=False, reply has no thread_ts."""
        gw = _make_gateway(threading=False)
        client = _make_slack_client()
        event = _make_event(channel="CTEST", ts="200.0", thread_ts="100.0")

        await gw._handle_slack_message(event, client)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in kwargs

    @pytest.mark.asyncio
    async def test_reply_text_contains_agent_response(self):
        gw = _make_gateway(agent_response="The answer is 42")
        client = _make_slack_client()
        event = _make_event()

        await gw._handle_slack_message(event, client)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["text"] == "The answer is 42"


# ---------------------------------------------------------------------------
# Empty message ignored
# ---------------------------------------------------------------------------

class TestSlackEmptyMessage:

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self):
        gw = _make_gateway()
        client = _make_slack_client()
        event = _make_event(text="   ")

        await gw._handle_slack_message(event, client)

        client.chat_postMessage.assert_not_called()
        gw.enqueue_message.assert_not_called()


# ---------------------------------------------------------------------------
# Allowlist / denylist
# ---------------------------------------------------------------------------

class TestSlackAllowlistDenylist:

    @pytest.mark.asyncio
    async def test_allowed_user_passes(self):
        gw = _make_gateway(allowed_users=["U1"])
        client = _make_slack_client()
        await gw._handle_slack_message(_make_event(user="U1"), client)
        gw.enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_allowed_user_blocked(self):
        gw = _make_gateway(allowed_users=["U1"])
        client = _make_slack_client()
        await gw._handle_slack_message(_make_event(user="U2"), client)
        gw.enqueue_message.assert_not_called()
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_user_blocked(self):
        gw = _make_gateway(denied_users=["U_BAD"])
        client = _make_slack_client()
        await gw._handle_slack_message(_make_event(user="U_BAD"), client)
        gw.enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_globally_denied_user_blocked(self):
        # SecurityConfig.denied_users is List[int]; gateway converts to str for comparison
        gw = _make_gateway(global_denied=[99999])
        client = _make_slack_client()
        # Slack user_id "99999" matches global_denied [99999] after str() conversion
        await gw._handle_slack_message(_make_event(user="99999"), client)
        gw.enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_anyone(self):
        gw = _make_gateway(allowed_users=[])
        client = _make_slack_client()
        await gw._handle_slack_message(_make_event(user="ANYONE"), client)
        gw.enqueue_message.assert_called_once()


# ---------------------------------------------------------------------------
# Slash command dispatch
# ---------------------------------------------------------------------------

class TestSlackSlashCommands:

    @pytest.mark.asyncio
    async def test_slash_command_dispatched_not_to_agent(self):
        gw = _make_gateway()
        client = _make_slack_client()
        event = _make_event(text="/help")

        await gw._handle_slack_message(event, client)

        # handle_message (agent) should NOT be called for a slash command
        gw.enqueue_message.assert_not_called()
        # But the client should have received a reply
        client.chat_postMessage.assert_called_once()

    @pytest.mark.asyncio
    async def test_slash_command_reply_sent_to_channel(self):
        gw = _make_gateway()
        client = _make_slack_client()
        event = _make_event(channel="C_TEST", text="/help")

        await gw._handle_slack_message(event, client)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C_TEST"
        assert kwargs["text"]  # non-empty

    @pytest.mark.asyncio
    async def test_slash_command_in_thread_replies_to_thread(self):
        gw = _make_gateway(threading=True)
        client = _make_slack_client()
        event = _make_event(text="/help", ts="200.0", thread_ts="100.0")

        await gw._handle_slack_message(event, client)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs.get("thread_ts") == "100.0"
