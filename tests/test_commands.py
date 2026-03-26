"""
Tests for pyclopse/core/commands.py:
  - CommandRegistry dispatch logic
  - Built-in commands: /help, /reset, /status, /model, /job delegation
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry():
    from pyclopse.core.commands import CommandRegistry
    return CommandRegistry()


def _make_context(gateway=None, session=None, sender_id="u1", channel="test"):
    from pyclopse.core.commands import CommandContext
    return CommandContext(
        gateway=gateway or MagicMock(),
        session=session,
        sender_id=sender_id,
        channel=channel,
    )


def _make_gateway_stub():
    """Minimal gateway stub with get_status() and _agent_manager."""
    gw = MagicMock()
    gw.get_status.return_value = {
        "is_running": True,
        "config_version": "1.0",
        "agents": {"total_agents": 1, "running_agents": 1},
        "sessions": {"active_sessions": 2, "total_sessions": 3},
        "jobs": {"total": 4, "running": 1},
    }
    gw._agent_manager = MagicMock()
    # _session_manager needs async create_session for cmd_reset
    sm = MagicMock()
    sm.create_session = AsyncMock(return_value=MagicMock(
        id="new-sess",
        agent_id="agent1",
        channel="test",
        user_id="u1",
        last_channel=None,
        last_user_id=None,
        last_thread_ts=None,
        save_metadata=MagicMock(),
    ))
    sm.set_active_session = MagicMock()
    gw._session_manager = sm
    return gw


def _make_session(session_id="sess-abc", agent_id="agent1"):
    session = MagicMock()
    session.id = session_id
    session.agent_id = agent_id
    session.context = {}
    session.channel = "test"
    session.user_id = "u1"
    session.last_channel = None
    session.last_user_id = None
    session.last_thread_ts = None
    return session


# ---------------------------------------------------------------------------
# CommandRegistry core
# ---------------------------------------------------------------------------

class TestCommandRegistry:

    @pytest.mark.asyncio
    async def test_dispatch_non_command_returns_none(self):
        reg = _make_registry()
        ctx = _make_context()
        result = await reg.dispatch("hello world", ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_unknown_command(self):
        reg = _make_registry()
        ctx = _make_context()
        result = await reg.dispatch("/unknown", ctx)
        # Unknown commands return None so callers can fall through to agent routing
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_calls_handler(self):
        reg = _make_registry()
        handler = AsyncMock(return_value="pong")
        reg.register("ping", handler, "test ping")
        ctx = _make_context()
        result = await reg.dispatch("/ping", ctx)
        assert result == "pong"
        handler.assert_called_once_with("", ctx)

    @pytest.mark.asyncio
    async def test_dispatch_passes_args(self):
        reg = _make_registry()
        captured = {}
        async def handler(args, ctx):
            captured["args"] = args
            return "ok"
        reg.register("echo", handler, "echo args")
        ctx = _make_context()
        await reg.dispatch("/echo foo bar baz", ctx)
        assert captured["args"] == "foo bar baz"

    @pytest.mark.asyncio
    async def test_dispatch_case_insensitive(self):
        reg = _make_registry()
        handler = AsyncMock(return_value="ok")
        reg.register("ping", handler, "test")
        ctx = _make_context()
        result = await reg.dispatch("/PING", ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_dispatch_handler_exception_returns_error(self):
        reg = _make_registry()
        async def boom(args, ctx):
            raise ValueError("oops")
        reg.register("boom", boom, "breaks")
        ctx = _make_context()
        result = await reg.dispatch("/boom", ctx)
        assert "Error" in result
        assert "boom" in result

    def test_help_text_lists_commands(self):
        reg = _make_registry()
        reg.register("foo", AsyncMock(), "does foo")
        reg.register("bar", AsyncMock(), "does bar")
        text = reg.help_text()
        assert "/foo" in text
        assert "/bar" in text
        assert "does foo" in text

    def test_help_text_empty_registry(self):
        reg = _make_registry()
        assert "No commands" in reg.help_text()


# ---------------------------------------------------------------------------
# Built-in: /help
# ---------------------------------------------------------------------------

class TestHelpCommand:

    @pytest.mark.asyncio
    async def test_help_lists_builtin_commands(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw)
        result = await reg.dispatch("/help", ctx)
        assert "/help" in result
        assert "/reset" in result
        assert "/status" in result
        assert "/model" in result
        assert "/job" in result


# ---------------------------------------------------------------------------
# Built-in: /reset
# ---------------------------------------------------------------------------

class TestResetCommand:

    @pytest.mark.asyncio
    async def test_reset_returns_success_message(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        session.history_dir = None  # no history dir in unit test
        agent = MagicMock()
        agent.evict_session_runner = AsyncMock()
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        result = await reg.dispatch("/reset", ctx)
        assert "✅" in result
        agent.evict_session_runner.assert_awaited_once_with(session.id)

    @pytest.mark.asyncio
    async def test_reset_archives_history_files(self, tmp_path):
        """cmd_reset archives history.json into archived/ subdirectory."""
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        session.history_dir = tmp_path
        session.message_count = 4
        # Create dummy history files
        (tmp_path / "history.json").write_text('{"messages":[]}')
        (tmp_path / "history_previous.json").write_text('{"messages":[]}')

        agent = MagicMock()
        agent.evict_session_runner = AsyncMock()
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        await reg.dispatch("/reset", ctx)

        # Originals moved to archived/
        assert not (tmp_path / "history.json").exists()
        archived = list((tmp_path / "archived").glob("history.json.*"))
        assert len(archived) == 1

    @pytest.mark.asyncio
    async def test_reset_no_session(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw, session=None)
        result = await reg.dispatch("/reset", ctx)
        assert "No active session" in result


# ---------------------------------------------------------------------------
# Built-in: /status
# ---------------------------------------------------------------------------

class TestStatusCommand:

    @pytest.mark.asyncio
    async def test_status_includes_key_fields(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw)
        result = await reg.dispatch("/status", ctx)
        assert "pyclopse" in result
        assert "Running" in result
        assert "Agents" in result
        assert "Sessions" in result
        assert "Jobs" in result

    @pytest.mark.asyncio
    async def test_status_calls_get_status(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw)
        await reg.dispatch("/status", ctx)
        gw.get_status.assert_called_once()


# ---------------------------------------------------------------------------
# Built-in: /model
# ---------------------------------------------------------------------------

class TestModelCommand:

    def _make_agent(self, base_model="sonnet"):
        agent = MagicMock()
        agent.config.model = base_model
        agent._session_runners = {}
        return agent

    @pytest.mark.asyncio
    async def test_model_show_current_default(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        agent = self._make_agent("haiku")
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        result = await reg.dispatch("/model", ctx)
        assert "haiku" in result
        assert "Current model" in result

    @pytest.mark.asyncio
    async def test_model_show_override(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        session.context["model_override"] = "opus"
        agent = self._make_agent("haiku")
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        result = await reg.dispatch("/model", ctx)
        assert "opus" in result

    @pytest.mark.asyncio
    async def test_model_set_stores_override(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        agent = self._make_agent()
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        result = await reg.dispatch("/model gpt-4o", ctx)
        assert session.context.get("model_override") == "gpt-4o"
        assert "✅" in result

    @pytest.mark.asyncio
    async def test_model_set_clears_session_runner(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)

        session = _make_session()
        agent = self._make_agent()
        agent._session_runners[session.id] = MagicMock()
        gw._agent_manager.get_agent.return_value = agent

        ctx = _make_context(gateway=gw, session=session)
        await reg.dispatch("/model claude-3", ctx)
        assert session.id not in agent._session_runners

    @pytest.mark.asyncio
    async def test_model_no_session(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw, session=None)
        result = await reg.dispatch("/model", ctx)
        assert "No active session" in result

    @pytest.mark.asyncio
    async def test_model_no_agent(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        gw._agent_manager.get_agent.return_value = None
        register_builtin_commands(reg, gw)
        session = _make_session()
        ctx = _make_context(gateway=gw, session=session)
        result = await reg.dispatch("/model", ctx)
        assert "No agent" in result


# ---------------------------------------------------------------------------
# Built-in: /job delegation
# ---------------------------------------------------------------------------

class TestJobDelegation:

    @pytest.mark.asyncio
    async def test_job_delegates_to_handle_job_command(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        gw._handle_job_command = AsyncMock(return_value="Job result")
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw)
        result = await reg.dispatch("/job list", ctx)
        assert result == "Job result"
        gw._handle_job_command.assert_called_once_with("/job list")

    @pytest.mark.asyncio
    async def test_job_bare_delegates(self):
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        reg = CommandRegistry()
        gw = _make_gateway_stub()
        gw._handle_job_command = AsyncMock(return_value="help text")
        register_builtin_commands(reg, gw)
        ctx = _make_context(gateway=gw)
        await reg.dispatch("/job", ctx)
        gw._handle_job_command.assert_called_once_with("/job")


# ---------------------------------------------------------------------------
# Agent model_override wiring
# ---------------------------------------------------------------------------

class TestAgentModelOverride:

    def _make_agent_obj(self, base_model="sonnet"):
        """Create a real Agent-like stub with _get_session_runner."""
        from unittest.mock import MagicMock, patch
        from pyclopse.core.agent import Agent
        from pyclopse.config.schema import AgentConfig

        cfg = AgentConfig(name="test", model=base_model)
        agent = Agent.__new__(Agent)
        object.__setattr__(agent, "id", "a1")
        object.__setattr__(agent, "name", "test")
        object.__setattr__(agent, "config", cfg)
        object.__setattr__(agent, "_session_runners", {})
        object.__setattr__(agent, "_logger", logging.getLogger("test"))

        # Create a minimal fake runner
        fake_runner = MagicMock()
        fake_runner.model = base_model
        fake_runner.servers = []
        fake_runner.tools_config = {}
        object.__setattr__(agent, "fast_agent_runner", fake_runner)
        return agent

    def test_get_session_runner_uses_override(self):
        agent = self._make_agent_obj("default-model")
        with patch("pyclopse.agents.runner.AgentRunner") as MockRunner:
            MockRunner.return_value = MagicMock()
            agent._get_session_runner("sess1", model_override="custom-model")
            assert "sess1" in agent._session_runners

    def test_get_session_runner_no_override_uses_base(self):
        agent = self._make_agent_obj("base-model")
        with patch("pyclopse.agents.runner.AgentRunner") as MockRunner:
            MockRunner.return_value = MagicMock()
            agent._get_session_runner("sess2")
            call_kwargs = MockRunner.call_args[1]
            assert call_kwargs["model"] == "base-model"

    def test_get_session_runner_with_override_uses_override(self):
        agent = self._make_agent_obj("base-model")
        with patch("pyclopse.agents.runner.AgentRunner") as MockRunner:
            MockRunner.return_value = MagicMock()
            agent._get_session_runner("sess3", model_override="override-model")
            call_kwargs = MockRunner.call_args[1]
            assert call_kwargs["model"] == "override-model"


import logging
