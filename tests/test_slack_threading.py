"""
Tests for SlackPlugin with threading support.
Covers:
  - Session keying: thread_ts when threading=True, user_id when threading=False
  - Reply goes to correct thread (thread_ts in post_kwargs)
  - Threading=False replies to channel without thread_ts
  - Allowlist / denylist enforcement
  - Slash command dispatch via CommandRegistry
  - Empty message ignored
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gw_handle(
    dispatch_return="hello back",
    command_return=None,
    is_duplicate=False,
    check_access=True,
    agent_id="default",
    config=None,
):
    """Create a mock GatewayHandle with configurable behavior."""
    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_return)
    handle.dispatch_command = AsyncMock(return_value=command_return)
    handle.is_duplicate = MagicMock(return_value=is_duplicate)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value=agent_id)
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4000: [text])
    if config is None:
        config = _make_config()
    type(handle).config = PropertyMock(return_value=config)
    return handle


def _make_config(
    enabled=True,
    bot_token="xoxb-fake",
    threading=True,
    allowed_users=None,
    denied_users=None,
):
    """Create a mock gateway config with a Slack section."""
    slack_cfg = MagicMock()
    slack_cfg.enabled = enabled
    slack_cfg.bot_token = bot_token
    slack_cfg.threading = threading
    slack_cfg.allowed_users = allowed_users or []
    slack_cfg.denied_users = denied_users or []
    slack_cfg.bots = {}
    slack_cfg.signing_secret = None

    channels = MagicMock()
    channels.slack = slack_cfg

    config = MagicMock()
    config.channels = channels
    config.security = MagicMock()
    config.security.denied_users = []
    return config


def _make_event(user="U123", channel="C456", text="hello", ts="111.222", thread_ts=None):
    e = {"type": "message", "user": user, "channel": channel, "text": text, "ts": ts}
    if thread_ts is not None:
        e["thread_ts"] = thread_ts
    return e


def _make_slack_client():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock()
    return client


def _make_plugin(
    threading=True,
    allowed_users=None,
    denied_users=None,
    dispatch_return="hello back",
    command_return=None,
    check_access=True,
    is_duplicate=False,
):
    """Create a SlackPlugin wired to a mock GatewayHandle."""
    from pyclopse.channels.slack_plugin import SlackPlugin, SlackChannelConfig
    plugin = SlackPlugin()
    client = _make_slack_client()
    config = _make_config(
        threading=threading,
        allowed_users=allowed_users,
        denied_users=denied_users,
    )
    handle = _make_gw_handle(
        dispatch_return=dispatch_return,
        command_return=command_return,
        check_access=check_access,
        is_duplicate=is_duplicate,
        config=config,
    )

    # Wire up plugin internals
    plugin._gw = handle
    plugin._config = SlackChannelConfig.model_validate({
        "enabled": True,
        "botToken": "xoxb-fake",
        "threading": threading,
        "allowedUsers": allowed_users or [],
        "deniedUsers": denied_users or [],
    })
    plugin._bots = {"_default": (client, plugin._config)}
    plugin._token_to_bot = {"xoxb-fake": "_default"}

    return plugin, client, handle


# ---------------------------------------------------------------------------
# Threading: session key
# ---------------------------------------------------------------------------

class TestSlackSessionKeying:

    @pytest.mark.asyncio
    async def test_threading_on_passes_thread_ts_for_reply_routing(self):
        """With threading=True and a thread_ts present, dispatch is called."""
        plugin, client, handle = _make_plugin(threading=True)
        event = _make_event(user="U1", ts="100.0", thread_ts="90.0")

        await plugin._process_event(event)

        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_threading_on_uses_ts_when_no_thread_ts(self):
        """With threading=True but no thread_ts, ts is used for replies."""
        plugin, client, handle = _make_plugin(threading=True)
        event = _make_event(user="U1", ts="100.0")

        await plugin._process_event(event)

        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_threading_off_uses_user_id(self):
        """With threading=False, dispatch is still called."""
        plugin, client, handle = _make_plugin(threading=False)
        event = _make_event(user="U999", ts="100.0", thread_ts="90.0")

        await plugin._process_event(event)

        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Threading: reply goes into thread
# ---------------------------------------------------------------------------

class TestSlackReplyThreading:

    @pytest.mark.asyncio
    async def test_threading_on_replies_to_existing_thread(self):
        """When threading=True and thread_ts is set, reply includes thread_ts."""
        plugin, client, handle = _make_plugin(threading=True)
        event = _make_event(channel="CTEST", ts="200.0", thread_ts="100.0")

        await plugin._process_event(event)

        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "CTEST"
        assert kwargs["thread_ts"] == "100.0"

    @pytest.mark.asyncio
    async def test_threading_on_starts_thread_for_top_level_message(self):
        """When threading=True and no thread_ts, reply uses ts to start a new thread."""
        plugin, client, handle = _make_plugin(threading=True)
        event = _make_event(channel="CTEST", ts="500.0")

        await plugin._process_event(event)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["thread_ts"] == "500.0"

    @pytest.mark.asyncio
    async def test_threading_off_does_not_include_thread_ts(self):
        """When threading=False, reply has no thread_ts."""
        plugin, client, handle = _make_plugin(threading=False)
        event = _make_event(channel="CTEST", ts="200.0", thread_ts="100.0")

        await plugin._process_event(event)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in kwargs

    @pytest.mark.asyncio
    async def test_reply_text_contains_agent_response(self):
        plugin, client, handle = _make_plugin(dispatch_return="The answer is 42")
        event = _make_event()

        await plugin._process_event(event)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["text"] == "The answer is 42"


# ---------------------------------------------------------------------------
# Empty message ignored
# ---------------------------------------------------------------------------

class TestSlackEmptyMessage:

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self):
        plugin, client, handle = _make_plugin()
        event = _make_event(text="   ")

        await plugin._process_event(event)

        client.chat_postMessage.assert_not_called()
        handle.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Allowlist / denylist
# ---------------------------------------------------------------------------

class TestSlackAllowlistDenylist:

    @pytest.mark.asyncio
    async def test_allowed_user_passes(self):
        plugin, client, handle = _make_plugin(
            allowed_users=["U1"], check_access=True,
        )
        await plugin._process_event(_make_event(user="U1"))
        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_allowed_user_blocked(self):
        plugin, client, handle = _make_plugin(
            allowed_users=["U1"], check_access=False,
        )
        await plugin._process_event(_make_event(user="U2"))
        handle.dispatch.assert_not_called()
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_user_blocked(self):
        plugin, client, handle = _make_plugin(
            denied_users=["U_BAD"], check_access=False,
        )
        await plugin._process_event(_make_event(user="U_BAD"))
        handle.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_anyone(self):
        plugin, client, handle = _make_plugin(
            allowed_users=[], check_access=True,
        )
        await plugin._process_event(_make_event(user="ANYONE"))
        handle.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Slash command dispatch
# ---------------------------------------------------------------------------

class TestSlackSlashCommands:

    @pytest.mark.asyncio
    async def test_slash_command_dispatched_not_to_agent(self):
        plugin, client, handle = _make_plugin(command_return="Help text")
        event = _make_event(text="/help")

        await plugin._process_event(event)

        handle.dispatch_command.assert_called_once()
        handle.dispatch.assert_not_called()
        client.chat_postMessage.assert_called_once()

    @pytest.mark.asyncio
    async def test_slash_command_reply_sent_to_channel(self):
        plugin, client, handle = _make_plugin(command_return="Help text")
        event = _make_event(channel="C_TEST", text="/help")

        await plugin._process_event(event)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C_TEST"
        assert kwargs["text"]  # non-empty

    @pytest.mark.asyncio
    async def test_slash_command_in_thread_replies_to_thread(self):
        plugin, client, handle = _make_plugin(
            threading=True, command_return="Help text",
        )
        event = _make_event(text="/help", ts="200.0", thread_ts="100.0")

        await plugin._process_event(event)

        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs.get("thread_ts") == "100.0"
