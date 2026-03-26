"""Main TUI Application for pyclopse using Textual."""

import asyncio
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Static, Log, Input
from textual.binding import Binding

from pyclopse.tui.screens import ChatScreen, AgentsScreen, SessionsScreen, StatusScreen, LogsScreen
from pyclopse.tui.widgets import AgentList, SessionList, StatusPanel

DEBUG_LOG = Path("/tmp/pyclopse_tui_debug.log")


from pyclopse.utils.time import now


def debug_write(msg: str) -> None:
    """Write debug message to file."""
    timestamp = now().strftime("%H:%M:%S.%f")[:-3]
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[APP][{timestamp}] {msg}\n")


debug_write("APP.PY LOADED")


class TUIApp(App):
    """Main TUI Application for pyclopse."""
    
    TITLE = "Pyclopse Gateway"
    SUB_TITLE = "Terminal User Interface"
    
    # Define key bindings
    BINDINGS = [
        Binding("c", "switch_chat", "Chat", show=True),
        Binding("a", "switch_agents", "Agents", show=True),
        Binding("s", "switch_sessions", "Sessions", show=True),
        Binding("l", "switch_logs", "Logs", show=True),
        Binding("t", "switch_status", "Status", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+q", "quit", "Quit"),
    ]
    
    CSS = """
    Screen {
        background: $surface;
    }
    
    #main-container {
        height: 100%;
    }
    
    #sidebar {
        width: 30;
        background: $panel;
        border-right: solid $border;
    }
    
    #content {
        width: 1fr;
    }
    
    .sidebar-section {
        height: auto;
        padding: 1;
    }
    
    .sidebar-button {
        width: 100%;
        margin: 1 0;
    }
    
    #status-bar {
        height: 3;
        background: $panel;
        border-top: solid $border;
        content-align: center middle;
    }
    
    .title-bar {
        height: 3;
        background: $primary;
        content-align: center middle;
        color: $text;
    }
    
    Log {
        height: 100%;
        border: solid $border;
    }
    
    /* Chat screen layout */
    #main-layout {
        height: 100%;
    }
    
    #sidebar {
        width: 25;
        min-width: 20;
    }
    
    #chat-area {
        height: 100%;
        layout: vertical;
    }
    
    #chat-history {
        height: 1fr;
        border: solid $border;
    }
    
    #input-area {
        height: auto;
        dock: bottom;
    }
    
    #input-area {
        height: auto;
        padding: 1;
        background: $panel;
        border-top: solid $border;
    }
    
    #chat-input {
        width: 1fr;
    }
    
    #send-button {
        width: auto;
    }

    Button {
        pointer: pointer;
    }
    
    #agent-list Button {
        pointer: pointer;
    }
    
    #agent-actions Button {
        pointer: pointer;
    }
    
    #session-actions Button {
        pointer: pointer;
    }
    """
    
    def __init__(self, gateway=None):
        super().__init__()
        self.gateway = gateway
        self._current_agent_id: Optional[str] = None
        self._current_session_id: Optional[str] = None
        debug_write(f"TUIApp.__init__ called with gateway={gateway}")
    
    def on_mount(self) -> None:
        """Called when app is mounted."""
        # Install all screens
        self._install_screens()
        
        # Start with chat screen
        self.push_screen("chat")
    
    def _install_screens(self) -> None:
        """Install all screens."""
        debug_write(f"_install_screens: self.gateway={self.gateway}")
        
        # Chat screen
        self.install_screen(
            ChatScreen(gateway=self.gateway, app=self),
            name="chat"
        )
        
        # Agents screen
        self.install_screen(
            AgentsScreen(gateway=self.gateway, app=self),
            name="agents"
        )
        
        # Sessions screen
        self.install_screen(
            SessionsScreen(gateway=self.gateway, app=self),
            name="sessions"
        )
        
        # Status screen
        self.install_screen(
            StatusScreen(gateway=self.gateway, app=self),
            name="status"
        )
        
        # Logs screen
        self.install_screen(
            LogsScreen(gateway=self.gateway, app=self),
            name="logs"
        )
    
    def action_switch_chat(self) -> None:
        """Switch to chat screen."""
        self.push_screen("chat")
    
    def action_switch_agents(self) -> None:
        """Switch to agents screen."""
        self.push_screen("agents")
    
    def action_switch_sessions(self) -> None:
        """Switch to sessions screen."""
        self.push_screen("sessions")
    
    def action_switch_logs(self) -> None:
        """Switch to logs screen."""
        self.push_screen("logs")
    
    def action_switch_status(self) -> None:
        """Switch to status screen."""
        self.push_screen("status")
    
    def action_quit(self) -> None:
        """Quit the application."""
        self.exit()
    
    def set_current_agent(self, agent_id: str) -> None:
        """Set the current agent."""
        self._current_agent_id = agent_id
    
    def get_current_agent(self) -> Optional[str]:
        """Get the current agent ID."""
        return self._current_agent_id
    
    def set_current_session(self, session_id: str) -> None:
        """Set the current session."""
        self._current_session_id = session_id
    
    def get_current_session(self) -> Optional[str]:
        """Get the current session ID."""
        return self._current_session_id


async def run_tui(gateway=None) -> None:
    """Run the TUI application."""
    debug_write(f"run_tui called with gateway={gateway}")
    app = TUIApp(gateway=gateway)
    await app.run_async()


if __name__ == "__main__":
    asyncio.run(run_tui())
