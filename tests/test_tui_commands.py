"""
Tests for TUI command routing and status bar logic.

These tests exercise the non-Textual parts of ChatScreen:
  - slash-command detection and dispatch via CommandRegistry
  - status bar data assembly
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gateway(messages_total=5, agent_id="main", agent_name="Main Agent"):
    """Return a minimal gateway stub."""
    from pyclopse.core.gateway import Gateway
    from pyclopse.config.schema import Config, AgentsConfig, SecurityConfig

    gw = Gateway.__new__(Gateway)
    gw._initialized = True
    gw._is_running = True
    gw._logger = MagicMock()
    gw._audit_logger = None
    gw._config = Config(agents=AgentsConfig(), security=SecurityConfig())

    # Usage counters
    gw._usage = {
        "messages_total": messages_total,
        "messages_by_agent": {agent_id: messages_total},
        "messages_by_channel": {"tui": messages_total},
        "started_at": time.time() - 120,
    }

    # Agent manager stub
    mock_agent = MagicMock()
    mock_agent.name = agent_name
    mock_am = MagicMock()
    mock_am.agents = {agent_id: mock_agent}
    gw._agent_manager = mock_am

    # Session manager stub
    mock_session = MagicMock()
    mock_session.id = "s1"
    mock_session.agent_id = agent_id
    mock_sm = MagicMock()
    mock_sm.get_or_create_session = AsyncMock(return_value=mock_session)
    gw._session_manager = mock_sm

    # Command registry
    from pyclopse.core.commands import CommandRegistry, register_builtin_commands
    gw._command_registry = CommandRegistry()
    register_builtin_commands(gw._command_registry, gw)

    return gw


# ---------------------------------------------------------------------------
# Command routing: slash-command detection
# ---------------------------------------------------------------------------

class TestSlashCommandDetection:
    """Verify that _send_message routes /commands differently from plain text."""

    def _make_screen(self, gateway):
        """Return a ChatScreen with mocked Textual internals."""
        from pyclopse.tui.screens import ChatScreen
        screen = ChatScreen.__new__(ChatScreen)
        screen.gateway = gateway
        screen._current_agent_id = "main"
        screen._chat_history = []   # will be overridden by mock
        screen._tag_buffer = ""
        screen._in_thinking = False
        screen._is_processing = False

        # Stub _append_chat so we can inspect calls
        screen._appended: list = []
        screen._append_chat = lambda text: screen._appended.append(text)

        # Stub _process_message so we know if it was called
        screen._process_calls: list = []
        screen._process_message = lambda msg: screen._process_calls.append(msg)

        # Stub _dispatch_command
        screen._dispatch_calls: list = []
        screen._dispatch_command = lambda msg: screen._dispatch_calls.append(msg)

        # Stub chat input
        mock_input = MagicMock()
        mock_input.value = ""
        screen._chat_input = mock_input

        return screen

    def test_plain_message_calls_process_message(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._send_message("hello world")
        assert "hello world" in screen._process_calls

    def test_slash_message_calls_dispatch_command(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._send_message("/help")
        assert "/help" in screen._dispatch_calls

    def test_slash_message_does_not_call_process_message(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._send_message("/status")
        assert screen._process_calls == []

    def test_plain_message_does_not_call_dispatch(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._send_message("just talking")
        assert screen._dispatch_calls == []

    def test_empty_message_does_nothing(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._send_message("   ")
        assert screen._process_calls == []
        assert screen._dispatch_calls == []

    def test_slash_without_gateway_does_not_dispatch(self):
        from pyclopse.tui.screens import ChatScreen
        screen = ChatScreen.__new__(ChatScreen)
        screen.gateway = None
        screen._current_agent_id = None
        screen._tag_buffer = ""
        screen._in_thinking = False
        screen._is_processing = False
        screen._appended = []
        screen._append_chat = lambda t: screen._appended.append(t)
        screen._dispatch_calls = []
        screen._dispatch_command = lambda m: screen._dispatch_calls.append(m)
        screen._process_calls = []
        screen._process_message = lambda m: screen._process_calls.append(m)
        mock_input = MagicMock()
        mock_input.value = ""
        screen._chat_input = mock_input

        # Without gateway, /help should fall through to the "no gateway" message path
        screen._send_message("/help")
        # Gateway is None so dispatch branch is skipped
        assert screen._dispatch_calls == []


# ---------------------------------------------------------------------------
# Slash command dispatch integration
# ---------------------------------------------------------------------------

class TestSlashCommandDispatch:

    @pytest.mark.asyncio
    async def test_help_command_returns_text(self):
        gw = _make_gateway()
        ctx_class = None
        from pyclopse.core.commands import CommandContext
        ctx = CommandContext(gateway=gw, session=None, sender_id="tui_user", channel="tui")
        result = await gw._command_registry.dispatch("/help", ctx)
        assert result is not None
        assert "help" in result.lower() or "/" in result

    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self):
        gw = _make_gateway()
        from pyclopse.core.commands import CommandContext
        ctx = CommandContext(gateway=gw, session=None, sender_id="tui_user", channel="tui")
        result = await gw._command_registry.dispatch("/notacommand", ctx)
        # Unknown commands return None so callers can fall through to agent routing
        assert result is None

    @pytest.mark.asyncio
    async def test_status_command_returns_text(self):
        gw = _make_gateway()
        from pyclopse.core.commands import CommandContext
        ctx = CommandContext(gateway=gw, session=None, sender_id="tui_user", channel="tui")
        result = await gw._command_registry.dispatch("/status", ctx)
        assert result is not None


# ---------------------------------------------------------------------------
# Status bar data assembly
# ---------------------------------------------------------------------------

class TestStatusBarData:
    """Verify status bar content is correctly assembled from gateway state."""

    def _make_screen(self, gateway, agent_id="main"):
        from pyclopse.tui.screens import ChatScreen
        screen = ChatScreen.__new__(ChatScreen)
        screen.gateway = gateway
        screen._current_agent_id = agent_id
        screen._is_processing = False
        # Capture what the status bar would be set to
        screen._status_updates: list = []
        mock_bar = MagicMock()
        mock_bar.update = lambda txt: screen._status_updates.append(txt)
        screen._status_bar = mock_bar
        return screen

    def test_status_bar_shows_agent_name(self):
        gw = _make_gateway(agent_name="CodeBot")
        screen = self._make_screen(gw)
        screen._update_status_bar()
        assert screen._status_updates
        assert "CodeBot" in screen._status_updates[-1]

    def test_status_bar_shows_message_count(self):
        gw = _make_gateway(messages_total=42)
        screen = self._make_screen(gw)
        screen._update_status_bar()
        assert "42" in screen._status_updates[-1]

    def test_status_bar_shows_uptime(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._update_status_bar()
        # Uptime format HH:MM:SS should be in output
        import re
        assert re.search(r"\d+:\d\d:\d\d", screen._status_updates[-1])

    def test_status_bar_shows_processing_when_active(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._is_processing = True
        screen._update_status_bar()
        assert "Processing" in screen._status_updates[-1]

    def test_status_bar_no_processing_indicator_when_idle(self):
        gw = _make_gateway()
        screen = self._make_screen(gw)
        screen._is_processing = False
        screen._update_status_bar()
        assert "Processing" not in screen._status_updates[-1]
