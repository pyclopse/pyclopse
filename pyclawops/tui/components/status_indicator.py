"""Status indicator component."""

from typing import Optional

from textual.widgets import Static
from textual.app import ComposeResult


class StatusIndicator(Static):
    """A status indicator widget showing running/stopped state."""
    
    DEFAULT_CSS = """
    StatusIndicator {
        height: 3;
        width: auto;
        content-align: center middle;
        padding: 0 2;
    }
    
    StatusIndicator.running {
        background: $success;
        color: $text;
    }
    
    StatusIndicator.stopped {
        background: $error;
        color: $text;
    }
    
    StatusIndicator.pending {
        background: $warning;
        color: $text;
    }
    """
    
    def __init__(
        self,
        status: str = "stopped",
        label: str = "",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.status = status
        self.label = label
        self._update_class()
    
    def compose(self) -> ComposeResult:
        """Compose the status indicator."""
        self._update_display()
        yield Static("", id="status-text")
    
    def set_status(self, status: str) -> None:
        """Set the status."""
        self.status = status
        self._update_class()
        self._update_display()
    
    def set_label(self, label: str) -> None:
        """Set the status label."""
        self.label = label
        self._update_display()
    
    def _update_class(self) -> None:
        """Update the CSS class."""
        # Remove all status classes
        self.remove_class("running", "stopped", "pending")
        # Add current status class
        self.add_class(self.status)
    
    def _update_display(self) -> None:
        """Update the display text."""
        status_symbols = {
            "running": "●",
            "stopped": "○",
            "pending": "◐",
        }
        symbol = status_symbols.get(self.status, "○")
        
        text = f"{symbol} {self.label}" if self.label else symbol
        text_elem = self.query_one("#status-text", Static)
        text_elem.update(text)


class AgentStatusIndicator(StatusIndicator):
    """Specialized status indicator for agents."""
    
    def __init__(self, agent_name: str = "", agent_id: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.label = agent_name or agent_id
    
    def update_agent(self, name: str, agent_id: str, is_running: bool) -> None:
        """Update agent info."""
        self.agent_name = name
        self.agent_id = agent_id
        self.label = name or agent_id
        self.set_status("running" if is_running else "stopped")


class SessionStatusIndicator(StatusIndicator):
    """Specialized status indicator for sessions."""
    
    def __init__(self, session_id: str = "", message_count: int = 0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = session_id
        self.message_count = message_count
    
    def update_session(self, session_id: str, message_count: int, is_active: bool) -> None:
        """Update session info."""
        self.session_id = session_id
        self.message_count = message_count
        self.label = f"{session_id[:8]}... ({message_count} msgs)"
        self.set_status("running" if is_active else "stopped")
