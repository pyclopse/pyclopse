"""
Tests for:
  1. Bug #2 fix: _handle_with_fastagent passes str not list, per-session isolation
  2. Telegram incoming: message routing, allowed_users, response dispatch
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to build lightweight Agent / Session / IncomingMessage instances
# without touching FastAgent or the full Gateway init
# ---------------------------------------------------------------------------

from pyclaw.config.schema import AgentConfig
from pyclaw.core.session import Session
from pyclaw.core.router import IncomingMessage


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
    from pyclaw.core.agent import Agent

    if config is None:
        config = _make_agent_config()

    # Build with no session_manager so we don't need a DB
    with patch("pyclaw.core.agent.FASTAGENT_AVAILABLE", False):
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            result = agent._get_session_runner("session-abc-123")

        MockRunner.assert_called_once()
        assert result is mock_runner_instance
        assert "session-abc-123" in agent._session_runners

    def test_returns_same_runner_for_same_session(self):
        """_get_session_runner returns the cached runner on repeated calls."""
        base_runner = _make_mock_runner()
        agent = _make_agent(fast_agent_runner=base_runner)

        mock_runner_instance = _make_mock_runner()

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance):
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

        with patch("pyclaw.agents.runner.AgentRunner", side_effect=side_effect):
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            agent._get_session_runner("session-12345678-extra")

        kwargs = MockRunner.call_args.kwargs
        # agent_name should be "{agent_name}-{first 8 chars of session_id}"
        # session id "session-12345678-extra"[:8] == "session-"
        assert kwargs["agent_name"].startswith("test-agent-")
        assert kwargs["agent_name"] == "test-agent-session-"
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance):
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance):
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
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

        with patch("pyclaw.agents.runner.AgentRunner", return_value=mock_runner_instance) as MockRunner:
            await agent._handle_with_fastagent("hello", session)

        _, kwargs = MockRunner.call_args
        assert kwargs.get("history_path") is None


# ===========================================================================
# Section 2: Telegram incoming routing
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
    return msg


def _make_gateway_for_telegram(allowed_users: Optional[List[int]] = None):
    """
    Build a Gateway instance with all internals stubbed out,
    configured for Telegram tests.
    """
    from pyclaw.core.gateway import Gateway
    from pyclaw.config.schema import (
        Config,
        ChannelsConfig,
        TelegramConfig,
        AgentsConfig,
        ConcurrencyConfig,
    )

    gw = Gateway.__new__(Gateway)  # Skip __init__

    # Minimal internal state
    gw._is_running = True
    gw._initialized = True
    gw._logger = MagicMock()
    gw._audit_logger = None
    gw._telegram_bot = AsyncMock()
    gw._telegram_chat_id = None
    gw._telegram_polling_task = None
    gw._active_tasks = {}
    gw._session_manager = None
    gw._agent_manager = None
    gw._channels = {}
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    gw._hook_registry = None
    gw._known_session_ids = set()

    # Build a real-ish config with Telegram enabled
    telegram_cfg = TelegramConfig(
        enabled=True,
        botToken="fake-token",
        allowedUsers=allowed_users or [],
    )
    channels_cfg = ChannelsConfig(telegram=telegram_cfg)
    agents_cfg = AgentsConfig()

    gw._config = Config(channels=channels_cfg, agents=agents_cfg)

    return gw


class TestTelegramIncoming:
    """Tests for _handle_telegram_message and _telegram_poll."""

    @pytest.mark.asyncio
    async def test_routes_to_handle_message(self):
        """_handle_telegram_message calls handle_message with correct args."""
        gw = _make_gateway_for_telegram(allowed_users=[42])
        gw.handle_message = AsyncMock(return_value="bot reply")

        msg = _make_telegram_message(user_id=42, chat_id=42, text="Hi!")
        await gw._handle_telegram_message(msg)

        gw.handle_message.assert_called_once_with(
            channel="telegram",
            sender="Alice",
            sender_id="42",
            content="Hi!",
            message_id="101",
            agent_id="default",
        )

    @pytest.mark.asyncio
    async def test_sends_response_to_correct_chat(self):
        """_handle_telegram_message sends the response back to the right chat_id."""
        gw = _make_gateway_for_telegram(allowed_users=[42])
        gw.handle_message = AsyncMock(return_value="response text")

        msg = _make_telegram_message(user_id=42, chat_id=99, text="ping")
        await gw._handle_telegram_message(msg)

        gw._telegram_bot.send_message.assert_called_once_with(
            chat_id="99",
            text="response text",
        )

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self):
        """_handle_telegram_message ignores messages from users not in allowed_users."""
        gw = _make_gateway_for_telegram(allowed_users=[100, 200])
        gw.handle_message = AsyncMock(return_value="should not be called")

        msg = _make_telegram_message(user_id=42, chat_id=42, text="intruder")
        await gw._handle_telegram_message(msg)

        gw.handle_message.assert_not_called()
        gw._telegram_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_allowed_users_restriction_allows_anyone(self):
        """When allowed_users is empty, all users are accepted."""
        gw = _make_gateway_for_telegram(allowed_users=[])
        gw.handle_message = AsyncMock(return_value="welcome")

        msg = _make_telegram_message(user_id=9999, chat_id=9999, text="hey")
        await gw._handle_telegram_message(msg)

        gw.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_in_handle_message_sends_error_reply(self):
        """When handle_message raises, an error message is sent to the user."""
        gw = _make_gateway_for_telegram(allowed_users=[42])
        gw.handle_message = AsyncMock(side_effect=RuntimeError("boom"))

        msg = _make_telegram_message(user_id=42, chat_id=55, text="crash me")
        await gw._handle_telegram_message(msg)

        # Should have called send_message with an error message
        gw._telegram_bot.send_message.assert_called_once()
        call_kwargs = gw._telegram_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == "55"
        assert "boom" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_reply_when_handle_message_returns_none(self):
        """_handle_telegram_message does not call send_message if response is None."""
        gw = _make_gateway_for_telegram(allowed_users=[42])
        gw.handle_message = AsyncMock(return_value=None)

        msg = _make_telegram_message(user_id=42, chat_id=42, text="silence")
        await gw._handle_telegram_message(msg)

        gw._telegram_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_poll_dispatches_tasks(self):
        """_telegram_poll creates a task per incoming text message."""
        gw = _make_gateway_for_telegram()

        # Build a mock update
        update = MagicMock()
        update.update_id = 10
        update.message = _make_telegram_message(text="update text")

        # get_updates returns one batch then stops the loop
        call_count = 0

        async def fake_get_updates(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [update]
            # Stop the loop after first batch
            gw._is_running = False
            return []

        gw._telegram_bot.get_updates = fake_get_updates
        gw._handle_telegram_message = AsyncMock(return_value=None)

        # Run the poll loop
        await gw._telegram_poll()

        # The message handler should have been dispatched
        await asyncio.sleep(0)  # let any pending tasks run

    @pytest.mark.asyncio
    async def test_telegram_poll_stops_when_not_running(self):
        """_telegram_poll exits its loop when _is_running is False."""
        gw = _make_gateway_for_telegram()
        gw._is_running = False  # Already stopped

        get_updates_mock = AsyncMock(return_value=[])
        gw._telegram_bot.get_updates = get_updates_mock

        # Should exit immediately without looping
        await gw._telegram_poll()

        get_updates_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_poll_handles_exception_and_continues(self):
        """_telegram_poll logs errors and keeps running after transient failures."""
        gw = _make_gateway_for_telegram()

        call_count = 0

        async def fake_get_updates(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network hiccup")
            # Stop after second call
            gw._is_running = False
            return []

        gw._telegram_bot.get_updates = fake_get_updates

        # Should not raise
        with patch("asyncio.sleep", new=AsyncMock()):
            await gw._telegram_poll()

        assert call_count == 2  # Tried again after the error

    @pytest.mark.asyncio
    async def test_telegram_poll_cancelled_error_stops_loop(self):
        """_telegram_poll exits cleanly on CancelledError."""
        gw = _make_gateway_for_telegram()

        async def raise_cancelled(**kwargs):
            raise asyncio.CancelledError()

        gw._telegram_bot.get_updates = raise_cancelled

        # Should not propagate CancelledError
        await gw._telegram_poll()


# ===========================================================================
# Section 3: handle_message agent lookup
# ===========================================================================

class TestHandleMessageAgentLookup:
    """Test that handle_message uses first available agent, not 'default'."""

    @pytest.mark.asyncio
    async def test_uses_first_agent_not_hardcoded_default(self):
        """handle_message picks the first key from agent_manager.agents."""
        from pyclaw.core.gateway import Gateway
        from pyclaw.config.schema import (
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
        mock_session_manager = MagicMock()
        mock_session_manager.get_or_create_session = AsyncMock(return_value=mock_session)
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

        # Verify get_or_create_session was called with "myagent", not "default"
        mock_session_manager.get_or_create_session.assert_called_once()
        call_kwargs = mock_session_manager.get_or_create_session.call_args.kwargs
        assert call_kwargs["agent_id"] == "myagent"

        assert result == "reply from myagent"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_agents(self):
        """handle_message uses 'default' as agent_id when agent_manager is empty."""
        from pyclaw.core.gateway import Gateway
        from pyclaw.config.schema import Config

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
        gw._config = Config()

        # No agents at all
        mock_agent_manager = MagicMock()
        mock_agent_manager.agents = {}
        gw._agent_manager = mock_agent_manager

        mock_session_manager = MagicMock()
        mock_session_manager.get_or_create_session = AsyncMock(return_value=None)
        gw._session_manager = mock_session_manager

        result = await gw.handle_message(
            channel="telegram",
            sender="Bob",
            sender_id="7",
            content="test",
        )

        mock_session_manager.get_or_create_session.assert_called_once()
        call_kwargs = mock_session_manager.get_or_create_session.call_args.kwargs
        assert call_kwargs["agent_id"] == "default"

        # Session was None → returns a "Could not create session" message
        assert result == "Could not create session"
