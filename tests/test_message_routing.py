"""
Tests for:
  1. Bug #2 fix: _handle_with_fastagent passes str not list, per-session isolation
  2. Telegram incoming: message routing, allowed_users, response dispatch
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to build lightweight Agent / Session / IncomingMessage instances
# without touching FastAgent or the full Gateway init
# ---------------------------------------------------------------------------

from pyclopse.config.schema import AgentConfig
from pyclopse.core.session import Session
from pyclopse.core.router import IncomingMessage


def _make_agent_config(**kwargs) -> AgentConfig:
    """Create a minimal AgentConfig with sensible defaults."""
    defaults = dict(
        name="test-agent",
        model="sonnet",
        temperature=0.7,
        max_tokens=1024,
        system_prompt="You are a test assistant.",
        use_fastagent=False,
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def _make_session(session_id: str = "test-session-id-abcdefgh") -> Session:
    return Session(
        id=session_id,
        agent_id="agent-1",
        channel="telegram",
        user_id="user-42",
    )


def _make_incoming_message(content: str = "hello") -> IncomingMessage:
    return IncomingMessage(
        id="msg-1",
        channel="telegram",
        sender="Alice",
        sender_id="42",
        content=content,
    )


def _make_mock_runner(response: str = "mocked response") -> MagicMock:
    """Return a mock AgentRunner with the right attributes."""
    runner = MagicMock()
    runner.model = "sonnet"
    runner.servers = []
    runner.tools_config = {}
    runner.run = AsyncMock(return_value=response)
    runner.initialize = AsyncMock()
    return runner


# ---------------------------------------------------------------------------
# We need to build an Agent without triggering FastAgent initialisation.
# We patch `_init_fastagent` and `FASTAGENT_AVAILABLE` to be safe.
# ---------------------------------------------------------------------------

def _make_agent(fast_agent_runner=None, config=None):
    """Build an Agent dataclass instance with mocked internals."""
    from pyclopse.core.agent import Agent

    if config is None:
        config = _make_agent_config()

    # Build with no session_manager so we don't need a DB
    with patch("pyclopse.core.agent.FASTAGENT_AVAILABLE", False):
        agent = Agent(
            id="agent-1",
            name="test-agent",
            config=config,
        )

    # Inject the runner directly (bypassing __post_init__ which is already done)
    object.__setattr__(agent, "fast_agent_runner", fast_agent_runner)
    object.__setattr__(agent, "_session_runners", {})
    return agent


# ===========================================================================
# Section 1: Bug #2 – per-session runner isolation
# ===========================================================================

class TestPerSessionRunners:
    """Tests for _get_session_runner isolation."""

    def test_creates_runner_for_new_session(self):
        """_get_session_runner creates a new runner for an unknown session."""
        base_runner = _make_mock_runner()
        agent = _make_agent(fast_agent_runner=base_runner)

        mock_runner_instance = _make_mock_runner()

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            result = agent._get_session_runner("session-abc-123")

        MockRunner.assert_called_once()
        assert result is mock_runner_instance
        assert "session-abc-123" in agent._session_runners

    def test_returns_same_runner_for_same_session(self):
        """_get_session_runner returns the cached runner on repeated calls."""
        base_runner = _make_mock_runner()
        agent = _make_agent(fast_agent_runner=base_runner)

        mock_runner_instance = _make_mock_runner()

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance):
            first = agent._get_session_runner("session-xyz")
            second = agent._get_session_runner("session-xyz")

        assert first is second

    def test_different_sessions_get_different_runners(self):
        """Two different session IDs produce two distinct runner instances."""
        base_runner = _make_mock_runner()
        agent = _make_agent(fast_agent_runner=base_runner)

        runner_a = _make_mock_runner()
        runner_b = _make_mock_runner()

        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return runner_a if call_count == 1 else runner_b

        with patch("pyclopse.agents.runner.AgentRunner", side_effect=side_effect):
            result_a = agent._get_session_runner("session-aaa")
            result_b = agent._get_session_runner("session-bbb")

        assert result_a is not result_b
        assert result_a is runner_a
        assert result_b is runner_b

    def test_raises_when_no_fast_agent_runner(self):
        """_get_session_runner raises RuntimeError when fast_agent_runner is None."""
        agent = _make_agent(fast_agent_runner=None)

        with pytest.raises(RuntimeError, match="no FastAgent runner configured"):
            agent._get_session_runner("any-session")

    def test_runner_created_with_correct_args(self):
        """_get_session_runner passes the right params to AgentRunner."""
        base_runner = _make_mock_runner()
        base_runner.model = "gpt-4"
        base_runner.servers = ["mcp-server-1"]
        base_runner.tools_config = {"profile": "coding"}

        config = _make_agent_config(temperature=0.3, max_tokens=2048)
        agent = _make_agent(fast_agent_runner=base_runner, config=config)

        mock_runner_instance = _make_mock_runner()

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            agent._get_session_runner("session-12345678-extra")

        kwargs = MockRunner.call_args.kwargs
        # agent_name should be "{agent_name}-{last 6 chars of session_id}"
        # session id "session-12345678-extra"[-6:] == "-extra"
        assert kwargs["agent_name"].startswith("test-agent-")
        assert kwargs["agent_name"] == "test-agent--extra"
        assert kwargs["model"] == "gpt-4"
        assert kwargs["servers"] == ["mcp-server-1"]
        assert kwargs["tools_config"] == {"profile": "coding"}
        assert kwargs["temperature"] == 0.3
        assert kwargs["max_tokens"] == 2048


class TestHandleWithFastagent:
    """Tests that _handle_with_fastagent calls runner.run(str) not runner.run(list)."""

    @pytest.mark.asyncio
    async def test_calls_run_with_string_prompt(self):
        """_handle_with_fastagent passes a plain str to runner.run (bug fix)."""
        base_runner = _make_mock_runner(response="hello back")
        agent = _make_agent(fast_agent_runner=base_runner)

        session = _make_session("sess-aabbccdd")
        mock_runner_instance = _make_mock_runner(response="hello back")

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance):
            result = await agent._handle_with_fastagent("hello", session)

        # Verify run was called with a str, NOT a list
        mock_runner_instance.run.assert_called_once()
        call_arg = mock_runner_instance.run.call_args[0][0]
        assert isinstance(call_arg, str), (
            f"run() should receive str, got {type(call_arg)}"
        )
        assert call_arg == "hello"
        assert result == "hello back"

    @pytest.mark.asyncio
    async def test_handle_message_routes_to_fastagent_with_str(self):
        """Agent.handle_message calls _handle_with_fastagent with string content."""
        base_runner = _make_mock_runner(response="agent reply")
        agent = _make_agent(fast_agent_runner=base_runner)
        agent.is_running = True

        session = _make_session("sess-deadbeef")
        incoming = _make_incoming_message(content="What is 2+2?")

        mock_runner_instance = _make_mock_runner(response="agent reply")

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance):
            response = await agent.handle_message(incoming, session)

        assert response is not None
        assert response.content == "agent reply"

        # Confirm run() was called with a string
        mock_runner_instance.run.assert_called_once()
        arg = mock_runner_instance.run.call_args[0][0]
        assert isinstance(arg, str)
        assert arg == "What is 2+2?"

    @pytest.mark.asyncio
    async def test_handle_message_raises_without_runner(self):
        """handle_message returns an error OutgoingMessage if no runner."""
        agent = _make_agent(fast_agent_runner=None)
        session = _make_session()
        incoming = _make_incoming_message()

        response = await agent.handle_message(incoming, session)
        # Should return an error message, not raise
        assert response is not None
        assert "error" in response.content.lower() or "no FastAgent" in response.content


class TestHistoryInjection:
    """Tests for session history wiring: history_path passed to AgentRunner."""

    @pytest.mark.asyncio
    async def test_history_path_passed_to_new_runner(self, tmp_path):
        """_get_session_runner receives history_path from session.history_path."""
        from pathlib import Path

        base_runner = _make_mock_runner(response="ok")
        agent = _make_agent(fast_agent_runner=base_runner)
        session = _make_session("sess-history-test")
        session.history_dir = tmp_path  # sets history_path = tmp_path / "history.json"

        mock_runner_instance = _make_mock_runner(response="ok")

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            await agent._handle_with_fastagent("hello", session)

        # Verify AgentRunner was constructed with the history_path
        _, kwargs = MockRunner.call_args
        assert kwargs.get("history_path") == tmp_path / "history.json"

    @pytest.mark.asyncio
    async def test_existing_runner_reused_not_recreated(self):
        """Once a runner is cached, _handle_with_fastagent reuses it."""
        base_runner = _make_mock_runner(response="ok")
        agent = _make_agent(fast_agent_runner=base_runner)
        session = _make_session("sess-cached")
        # history_dir=None → history_path=None

        mock_runner_instance = _make_mock_runner(response="ok")

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            await agent._handle_with_fastagent("first", session)
            await agent._handle_with_fastagent("second", session)

        # AgentRunner constructor called only once — same instance reused
        assert MockRunner.call_count == 1

    @pytest.mark.asyncio
    async def test_no_history_path_when_session_has_no_dir(self):
        """When session has no history_dir, runner gets history_path=None."""
        base_runner = _make_mock_runner(response="ok")
        agent = _make_agent(fast_agent_runner=base_runner)
        session = _make_session("sess-no-hist")
        # history_dir defaults to None

        mock_runner_instance = _make_mock_runner(response="ok")

        with patch("pyclopse.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            await agent._handle_with_fastagent("hello", session)

        _, kwargs = MockRunner.call_args
        assert kwargs.get("history_path") is None


# ===========================================================================
# Section 2: Telegram incoming routing (via TelegramPlugin)
# ===========================================================================

def _make_telegram_message(
    user_id: int = 42,
    chat_id: int = 42,
    text: str = "Hello bot",
    first_name: str = "Alice",
    message_id: int = 101,
) -> MagicMock:
    """Build a mock telegram Message object."""
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = first_name
    msg.chat.id = chat_id
    msg.text = text
    msg.message_id = message_id
    msg.message_thread_id = None
    return msg


def _make_telegram_plugin(check_access: bool = True, dispatch_response: str = "bot reply"):
    """
    Build a TelegramPlugin with a mocked GatewayHandle, configured for tests.
    """
    from pyclopse.channels.telegram_plugin import TelegramPlugin, TelegramChannelConfig
    from pyclopse.config.schema import (
        Config,
        ChannelsConfig,
        AgentsConfig,
        SecurityConfig,
    )

    plugin = TelegramPlugin()

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    bot.send_chat_action = AsyncMock()
    plugin._bots = {"_default": bot}
    plugin._chat_ids = {"_default": None}

    telegram_cfg = TelegramChannelConfig.model_validate({
        "enabled": True,
        "botToken": "fake-token",
        "typingIndicator": False,  # disable to simplify tests
    })

    config = Config(
        channels=ChannelsConfig(telegram=telegram_cfg),
        agents=AgentsConfig(),
        security=SecurityConfig(),
    )

    handle = MagicMock()
    handle.dispatch = AsyncMock(return_value=dispatch_response)
    handle.dispatch_command = AsyncMock(return_value=None)
    handle.is_duplicate = MagicMock(return_value=False)
    handle.check_access = MagicMock(return_value=check_access)
    handle.resolve_agent_id = MagicMock(return_value="default")
    handle.register_endpoint = MagicMock()
    handle.split_message = MagicMock(side_effect=lambda text, limit=4096: [text])
    type(handle).config = PropertyMock(return_value=config)

    plugin._gw = handle
    plugin._telegram_config = telegram_cfg

    return plugin, bot, handle


class TestTelegramIncoming:
    """Tests for TelegramPlugin._handle_message."""

    @pytest.mark.asyncio
    async def test_routes_to_handle_message(self):
        """_handle_message calls dispatch with correct args."""
        plugin, bot, handle = _make_telegram_plugin(dispatch_response="bot reply")
        msg = _make_telegram_message(user_id=42, chat_id=42, text="Hi!")
        await plugin._handle_message(msg, "_default", bot)

        handle.dispatch.assert_called_once()
        call_kwargs = handle.dispatch.call_args.kwargs
        assert call_kwargs["channel"] == "telegram"
        assert call_kwargs["user_id"] == "42"
        assert call_kwargs["user_name"] == "Alice"
        assert call_kwargs["text"] == "Hi!"
        assert call_kwargs["message_id"] == "101"
        assert call_kwargs["agent_id"] == "default"

    @pytest.mark.asyncio
    async def test_sends_response_to_correct_chat(self):
        """_handle_message sends the response back to the right chat_id."""
        plugin, bot, handle = _make_telegram_plugin(dispatch_response="response text")
        msg = _make_telegram_message(user_id=42, chat_id=99, text="ping")
        await plugin._handle_message(msg, "_default", bot)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "99"
        assert call_kwargs["text"] == "response text"

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self):
        """_handle_message ignores messages from users not in allowed_users."""
        plugin, bot, handle = _make_telegram_plugin(check_access=False)
        msg = _make_telegram_message(user_id=42, chat_id=42, text="intruder")
        await plugin._handle_message(msg, "_default", bot)

        handle.dispatch.assert_not_called()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_allowed_users_restriction_allows_anyone(self):
        """When check_access returns True, all users are accepted."""
        plugin, bot, handle = _make_telegram_plugin(check_access=True, dispatch_response="welcome")
        msg = _make_telegram_message(user_id=9999, chat_id=9999, text="hey")
        await plugin._handle_message(msg, "_default", bot)

        handle.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_in_dispatch_sends_error_reply(self):
        """When dispatch raises, an error message is sent to the user."""
        plugin, bot, handle = _make_telegram_plugin()
        handle.dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        msg = _make_telegram_message(user_id=42, chat_id=55, text="crash me")
        await plugin._handle_message(msg, "_default", bot)

        # Should have called send_message with an error message
        bot.send_message.assert_called()
        # Find the error call (may follow other calls)
        found_error = False
        for c in bot.send_message.call_args_list:
            text = c.kwargs.get("text", "")
            if "boom" in text:
                assert c.kwargs["chat_id"] == "55"
                found_error = True
                break
        assert found_error, f"Expected error with 'boom' in send_message calls: {bot.send_message.call_args_list}"

    @pytest.mark.asyncio
    async def test_no_reply_when_dispatch_returns_none(self):
        """_handle_message does not call send_message if response is None."""
        plugin, bot, handle = _make_telegram_plugin(dispatch_response=None)
        handle.dispatch = AsyncMock(return_value=None)

        msg = _make_telegram_message(user_id=42, chat_id=42, text="silence")
        await plugin._handle_message(msg, "_default", bot)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_bot_dispatches_tasks(self):
        """_poll_bot creates a task per incoming text message."""
        from pyclopse.channels.telegram_plugin import TelegramPlugin

        plugin = TelegramPlugin()
        plugin._gw = MagicMock()

        # Build a mock update
        update = MagicMock()
        update.update_id = 10
        update.message = _make_telegram_message(text="update text")

        bot = AsyncMock()
        call_count = 0

        async def fake_get_updates(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [update]
            # Stop the loop after first batch by raising CancelledError
            raise asyncio.CancelledError()

        bot.get_updates = fake_get_updates
        plugin._handle_message = AsyncMock(return_value=None)

        # Run the poll loop (will exit on CancelledError)
        await plugin._poll_bot("_default", bot)

        # The message handler should have been dispatched
        await asyncio.sleep(0)  # let any pending tasks run

    @pytest.mark.asyncio
    async def test_poll_bot_cancelled_error_stops_loop(self):
        """_poll_bot exits cleanly on CancelledError."""
        from pyclopse.channels.telegram_plugin import TelegramPlugin

        plugin = TelegramPlugin()
        plugin._gw = MagicMock()

        bot = AsyncMock()

        async def raise_cancelled(**kwargs):
            raise asyncio.CancelledError()

        bot.get_updates = raise_cancelled

        # Should not propagate CancelledError
        await plugin._poll_bot("_default", bot)


# ===========================================================================
# Section 3: handle_message agent lookup
# ===========================================================================

class TestHandleMessageAgentLookup:
    """Test that handle_message uses first available agent, not 'default'."""

    @pytest.mark.asyncio
    async def test_uses_first_agent_not_hardcoded_default(self):
        """handle_message picks the first key from agent_manager.agents."""
        from pyclopse.core.gateway import Gateway
        from pyclopse.config.schema import (
            Config,
            ChannelsConfig,
            AgentsConfig,
            ConcurrencyConfig,
        )

        gw = Gateway.__new__(Gateway)
        gw._is_running = True
        gw._initialized = True
        gw._logger = MagicMock()
        gw._audit_logger = None
        gw._telegram_bot = None
        gw._telegram_chat_id = None
        gw._telegram_polling_task = None
        gw._channels = {}
        gw._hook_registry = None
        gw._known_session_ids = set()
        gw._known_endpoints = {}
        gw._agent_listeners = {}

        # Build config (no agents actually needed for this test)
        gw._config = Config()

        # Set up a mock agent_manager with one agent named "myagent" (not "default")
        mock_agent_manager = MagicMock()
        mock_agent_manager.agents = {"myagent": MagicMock()}
        gw._agent_manager = mock_agent_manager

        # Mock session manager
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.agent_id = "myagent"
        mock_session.context = {}
        mock_session_manager = MagicMock()
        mock_session_manager.get_active_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.set_active_session = MagicMock()
        gw._session_manager = mock_session_manager

        # Usage counters
        import time as _time
        gw._usage = {
            "messages_total": 0,
            "messages_by_agent": {},
            "messages_by_channel": {},
            "started_at": _time.time(),
        }

        # Mock the agent returned from get_agent
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "reply from myagent"
        mock_agent.handle_message = AsyncMock(return_value=mock_response)
        mock_agent_manager.get_agent = MagicMock(return_value=mock_agent)

        result = await gw.handle_message(
            channel="telegram",
            sender="Bob",
            sender_id="7",
            content="test",
        )

        # Verify get_active_session was called with "myagent", not "default"
        mock_session_manager.get_active_session.assert_called_once_with("myagent")

        assert result == "reply from myagent"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_agents(self):
        """handle_message uses 'default' as agent_id when agent_manager is empty."""
        from pyclopse.core.gateway import Gateway
        from pyclopse.config.schema import Config

        gw = Gateway.__new__(Gateway)
        gw._is_running = True
        gw._initialized = True
        gw._logger = MagicMock()
        gw._audit_logger = None
        gw._telegram_bot = None
        gw._telegram_chat_id = None
        gw._telegram_polling_task = None
        gw._channels = {}
        gw._hook_registry = None
        gw._known_session_ids = set()
        gw._known_endpoints = {}
        gw._config = Config()

        # No agents at all
        mock_agent_manager = MagicMock()
        mock_agent_manager.agents = {}
        gw._agent_manager = mock_agent_manager

        mock_session_manager = MagicMock()
        mock_session_manager.get_active_session = AsyncMock(return_value=None)
        mock_session_manager.create_session = AsyncMock(return_value=None)
        mock_session_manager.set_active_session = MagicMock()
        gw._session_manager = mock_session_manager

        result = await gw.handle_message(
            channel="telegram",
            sender="Bob",
            sender_id="7",
            content="test",
        )

        mock_session_manager.get_active_session.assert_called_once_with("default")

        # Session was None → returns a "Could not create session" message
        assert result == "Could not create session"
