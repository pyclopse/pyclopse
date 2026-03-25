"""Custom widgets for the pyclawops TUI."""

from typing import Any, Dict, List, Optional

from textual.widgets import Static, Button, DataTable
from textual.containers import Container, Horizontal, Vertical
from textual.app import ComposeResult


class AgentList(Static):
    """Widget to display and select agents."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._selected_agent: Optional[str] = None
    
    def compose(self) -> ComposeResult:
        """Compose the widget."""
        yield DataTable(id="agent-table")
    
    def on_mount(self) -> None:
        """Called when widget is mounted."""
        table = self.query_one("#agent-table", DataTable)
        table.add_columns("Status", "Name")
    
    def add_agent(self, agent_id: str, name: str, is_running: bool) -> None:
        """Add an agent to the list."""
        self._agents[agent_id] = {
            "name": name,
            "is_running": is_running,
        }
        self._refresh_table()
    
    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent."""
        self._agents.pop(agent_id, None)
        self._refresh_table()
    
    def update_agent_status(self, agent_id: str, is_running: bool) -> None:
        """Update agent status."""
        if agent_id in self._agents:
            self._agents[agent_id]["is_running"] = is_running
            self._refresh_table()
    
    def select_agent(self, agent_id: str) -> None:
        """Select an agent."""
        self._selected_agent = agent_id
    
    def get_selected(self) -> Optional[str]:
        """Get selected agent ID."""
        return self._selected_agent
    
    def _refresh_table(self) -> None:
        """Refresh the table."""
        table = self.query_one("#agent-table", DataTable)
        table.clear()
        
        for agent_id, info in self._agents.items():
            status = "🟢" if info["is_running"] else "🔴"
            table.add_row(status, info["name"])


class SessionList(Static):
    """Widget to display and manage sessions."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._selected_session: Optional[str] = None
    
    def compose(self) -> ComposeResult:
        """Compose the widget."""
        yield DataTable(id="session-table")
    
    def on_mount(self) -> None:
        """Called when widget is mounted."""
        table = self.query_one("#session-table", DataTable)
        table.add_columns("ID", "Agent", "Messages", "Status")
    
    def add_session(self, session_id: str, agent_id: str, message_count: int, is_active: bool) -> None:
        """Add a session to the list."""
        self._sessions[session_id] = {
            "agent_id": agent_id,
            "message_count": message_count,
            "is_active": is_active,
        }
        self._refresh_table()
    
    def remove_session(self, session_id: str) -> None:
        """Remove a session."""
        self._sessions.pop(session_id, None)
        self._refresh_table()
    
    def select_session(self, session_id: str) -> None:
        """Select a session."""
        self._selected_session = session_id
    
    def get_selected(self) -> Optional[str]:
        """Get selected session ID."""
        return self._selected_session
    
    def _refresh_table(self) -> None:
        """Refresh the table."""
        table = self.query_one("#session-table", DataTable)
        table.clear()
        
        for session_id, info in self._sessions.items():
            short_id = session_id[:8] + "..."
            status = "Active" if info["is_active"] else "Inactive"
            table.add_row(
                short_id,
                info["agent_id"],
                str(info["message_count"]),
                status,
            )


class StatusPanel(Static):
    """Widget to display status information."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._title: str = "Status"
        self._items: Dict[str, str] = {}
    
    def compose(self) -> ComposeResult:
        """Compose the widget."""
        yield Static("", id="status-content")
    
    def set_title(self, title: str) -> None:
        """Set the panel title."""
        self._title = title
        self._update_display()
    
    def set_item(self, key: str, value: str) -> None:
        """Set a status item."""
        self._items[key] = value
        self._update_display()
    
    def remove_item(self, key: str) -> None:
        """Remove a status item."""
        self._items.pop(key, None)
        self._update_display()
    
    def clear_items(self) -> None:
        """Clear all items."""
        self._items.clear()
        self._update_display()
    
    def _update_display(self) -> None:
        """Update the display."""
        lines = [f"[b]{self._title}[/b]", ""]
        for key, value in self._items.items():
            lines.append(f"{key}: {value}")
        
        content = self.query_one("#status-content", Static)
        content.update("\n".join(lines))


class MessageBubble(Static):
    """Widget to display a chat message bubble."""
    
    def __init__(
        self,
        role: str = "user",
        content: str = "",
        timestamp: str = "",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._role = role
        self._content = content
        self._timestamp = timestamp
    
    def compose(self) -> ComposeResult:
        """Compose the message bubble."""
        role_indicator = "You" if self._role == "user" else "Assistant"
        color = "blue" if self._role == "user" else "green"
        
        yield Static(
            f"[bold {color}]{role_indicator}[/bold {color}] {self._timestamp}\n"
            f"{self._content}",
            id="message-content",
        )


class ActionBar(Static):
    """Widget for action buttons."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._actions: List[Dict[str, str]] = []
    
    def compose(self) -> ComposeResult:
        """Compose the action bar."""
        with Horizontal(id="action-buttons"):
            pass
    
    def add_action(self, label: str, action_id: str, variant: str = "default") -> None:
        """Add an action button."""
        self._actions.append({
            "label": label,
            "action_id": action_id,
            "variant": variant,
        })
        self._refresh_actions()
    
    def remove_action(self, action_id: str) -> None:
        """Remove an action button."""
        self._actions = [a for a in self._actions if a["action_id"] != action_id]
        self._refresh_actions()
    
    def _refresh_actions(self) -> None:
        """Refresh action buttons."""
        container = self.query_one("#action-buttons", Horizontal)
        container.remove_children()
        
        for action in self._actions:
            container.mount(
                Button(
                    action["label"],
                    id=action["action_id"],
                    variant=action["variant"],
                )
            )


class ConfigEditor(Static):
    """Widget to edit configuration."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config: Dict[str, Any] = {}
    
    def compose(self) -> ComposeResult:
        """Compose the config editor."""
        yield DataTable(id="config-table")
    
    def on_mount(self) -> None:
        """Called when widget is mounted."""
        table = self.query_one("#config-table", DataTable)
        table.add_columns("Key", "Value", "Type")
    
    def load_config(self, config: Dict[str, Any]) -> None:
        """Load configuration."""
        self._config = config
        self._refresh_table()
    
    def get_config(self) -> Dict[str, Any]:
        """Get current configuration."""
        return self._config.copy()
    
    def set_value(self, key: str, value: Any) -> None:
        """Set a config value."""
        self._config[key] = value
        self._refresh_table()
    
    def _refresh_table(self) -> None:
        """Refresh the table."""
        table = self.query_one("#config-table", DataTable)
        table.clear()
        
        for key, value in self._config.items():
            value_type = type(value).__name__
            table.add_row(key, str(value), value_type)
