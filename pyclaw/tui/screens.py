"""Screens for the pyclaw TUI."""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Log,
    Static,
    Label,
    DataTable,
    Switch,
    TextArea,
)
from textual.binding import Binding
from textual import work

# Debug log file path
DEBUG_LOG = Path("/tmp/pyclaw_tui_debug.log")


def debug_write(msg: str) -> None:
    """Write debug message to file."""
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")


# Initial debug to confirm import
debug_write("SCREENS.PY LOADED")


class ChatScreen(Screen):
    """Interactive chat screen."""
    
    def __init__(self, gateway=None, app=None):
        super().__init__()
        self.gateway = gateway
        self.app_ref = app
        self._current_agent_id: Optional[str] = None
        self._chat_history: List[Dict[str, str]] = []
        debug_write(f"ChatScreen.__init__ called with gateway={gateway}")
    
    BINDINGS = [
        Binding("escape", "clear_input", "Clear"),
        Binding("ctrl+k", "switch_agent", "Switch Agent"),
    ]
    
    def compose(self) -> ComposeResult:
        """Compose the chat screen."""
        # Header with agent selector
        yield Header()
        
        with Horizontal(id="main-layout"):
            # Left sidebar - agent list
            with Vertical(id="sidebar", classes="sidebar"):
                yield Static("[b]Agents[/b]", id="sidebar-title")
                yield AgentListWidget(id="agent-list")
            
            # Main chat area
            with Vertical(id="chat-area"):
                # Chat history (TextArea for text selection support)
                yield TextArea(id="chat-history", read_only=True)
                
                # Input area
                with Horizontal(id="input-area"):
                    yield Input(
                        placeholder="Type your message...",
                        id="chat-input",
                        validate_on=["submitted"],
                    )
                    yield Button("Send", id="send-button", variant="primary")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when screen is mounted."""
        debug_write("ChatScreen.on_mount called")
        
        self._chat_input = self.query_one("#chat-input", Input)
        self._chat_history = self.query_one("#chat-history", TextArea)
        self._agent_list = self.query_one("#agent-list", AgentListWidget)
        
        debug_write(f"on_mount: gateway={self.gateway}, app_ref={self.app_ref}")
        
        # Load agents if gateway available
        if self.gateway:
            debug_write(f"on_mount: calling _load_agents, agent_manager={getattr(self.gateway, 'agent_manager', 'NONE')}")
            self._load_agents()
        else:
            debug_write("on_mount: No gateway!")
        
        # Focus input
        self._chat_input.focus()
    
    def _load_agents(self) -> None:
        """Load agents from gateway."""
        debug_write("_load_agents called")
        
        if self.gateway and self.gateway.agent_manager:
            agents = self.gateway.agent_manager.list_agents()
            debug_write(f"_load_agents: Found {len(agents)} agents: {[a.id for a in agents]}")
            
            for agent in agents:
                self._agent_list.add_agent(agent.id, agent.name, agent.is_running)
            
            # Auto-select agent with provider (prefer "main" or first with provider)
            if agents and not self._current_agent_id:
                # Prefer "main" agent if exists
                main_agent = next((a for a in agents if a.id == "main"), None)
                if main_agent:
                    self._current_agent_id = main_agent.id
                else:
                    # Fall back to first agent that has a provider
                    with_provider = next((a for a in agents if a.provider is not None), None)
                    if with_provider:
                        self._current_agent_id = with_provider.id
                    else:
                        # Last resort: first agent
                        self._current_agent_id = agents[0].id
                
                debug_write(f"_load_agents: Selected agent {self._current_agent_id}")
                # Also update the app's current agent
                if self.app_ref:
                    self.app_ref.set_current_agent(self._current_agent_id)
        # Fallback: use "default" agent if no agents loaded
        elif self.gateway and not self._current_agent_id:
            debug_write("_load_agents: No agents found, using 'default'")
            self._current_agent_id = "default"
            if self.app_ref:
                self.app_ref.set_current_agent(self._current_agent_id)
        else:
            debug_write(f"_load_agents: gateway={self.gateway}, agent_manager={getattr(self.gateway, 'agent_manager', 'NONE') if self.gateway else 'N/A'}")
    
    def _append_chat(self, text: str) -> None:
        """Append text to chat history (strip Rich tags for TextArea)."""
        # Strip Rich markup tags ([/blue], [green], etc.) for TextArea
        plain = re.sub(r'\[/?\w+\]', '', text)
        # Append to TextArea
        current = self._chat_history.text
        if current:
            self._chat_history.text = current + "\n" + plain
        else:
            self._chat_history.text = plain
        # Auto-scroll to bottom
        self._chat_history.scroll_to_row(self._chat_history.line_count - 1)
    
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle input submission."""
        if event.input.id == "chat-input":
            self._send_message(event.value)
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "send-button":
            message = self._chat_input.value
            if message:
                self._send_message(message)
    
    def _send_message(self, message: str) -> None:
        """Send a message."""
        debug_write(f"_send_message: gateway={self.gateway}, current_agent_id={self._current_agent_id}")
        
        if not message.strip():
            return
        
        # Add user message to history
        self._append_chat(f"[blue]You:[/blue] {message}")
        
        # Clear input
        self._chat_input.value = ""
        
        # If gateway available, process message
        if self.gateway and self._current_agent_id:
            self._process_message(message)
        elif self.gateway:
            # Gateway exists but no agent configured - use demo mode
            self._append_chat(f"[yellow]PyClaw:[/yellow] Gateway running! Configure agents in config to enable chat.")
        else:
            # No gateway - demo mode
            self._append_chat(f"[yellow]PyClaw:[/yellow] Gateway not connected. Start with --tui flag.")
    
    @work(exclusive=True)
    async def _process_message(self, message: str) -> None:
        """Process message through gateway."""
        try:
            # Get or create session
            session = None
            if self.gateway.session_manager:
                session = await self.gateway.session_manager.get_or_create_session(
                    agent_id=self._current_agent_id,
                    channel="tui",
                    user_id="tui_user",
                )
            
            if not session:
                self._append_chat("[red]Error:[/red] Could not create session")
                return
            
            # Get agent
            agent = self.gateway.agent_manager.get_agent(self._current_agent_id)
            if not agent:
                self._append_chat("[red]Error:[/red] Agent not found")
                return
            
            # Create incoming message
            from pyclaw.core.router import IncomingMessage
            incoming = IncomingMessage(
                id="tui_msg",
                content=message,
                channel="tui",
                sender="tui_user",
                sender_id="tui_user",
            )
            
            # Handle message
            response = await agent.handle_message(incoming, session)
            
            # Display response
            if response:
                self._append_chat(f"[green]{agent.name}:[/green] {response.content}")
            else:
                self._append_chat("[yellow]No response[/yellow]")
                
        except Exception as e:
            self._append_chat(f"[red]Error:[/red] {str(e)}")
    
    def action_clear_input(self) -> None:
        """Clear the input field."""
        self._chat_input.value = ""
    
    def action_switch_agent(self) -> None:
        """Switch to agent selection."""
        self.app_ref.push_screen("agents")


class AgentsScreen(Screen):
    """Agent management screen."""
    
    def __init__(self, gateway=None, app=None):
        super().__init__()
        self.gateway = gateway
        self.app_ref = app
    
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select_agent", "Select"),
        Binding("escape", "go_back", "Back"),
    ]
    
    def compose(self) -> ComposeResult:
        """Compose the agents screen."""
        yield Header()
        
        with Vertical(id="agents-container"):
            yield Static("[b]Agent Management[/b]", id="agents-title")
            
            # Agent table
            yield DataTable(id="agents-table")
            
            # Action buttons
            with Horizontal(id="agent-actions"):
                yield Button("Start", id="start-agent", variant="success")
                yield Button("Stop", id="stop-agent", variant="error")
                yield Button("Refresh", id="refresh-agents")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self._table = self.query_one("#agents-table", DataTable)
        self._table.add_columns("ID", "Name", "Status", "Model")
        self._load_agents()
    
    def _load_agents(self) -> None:
        """Load agents from gateway."""
        self._table.clear()
        
        if self.gateway and self.gateway.agent_manager:
            agents = self.gateway.agent_manager.list_agents()
            for agent in agents:
                status = "Running" if agent.is_running else "Stopped"
                self._table.add_row(agent.id, agent.name, status, agent.config.model)
    
    def action_refresh(self) -> None:
        """Refresh agent list."""
        self._load_agents()
    
    def action_select_agent(self) -> None:
        """Select an agent."""
        # Get selected row
        selected = self._table.cursor_row
        if selected is not None:
            row = self._table.get_row_at(selected)
            if row:
                agent_id = row[0]
                if self.app_ref:
                    self.app_ref.set_current_agent(agent_id)
                self.app_ref.push_screen("chat")
    
    def action_go_back(self) -> None:
        """Go back to previous screen."""
        self.app_ref.pop_screen()
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        selected = self._table.cursor_row
        if selected is None:
            return
        
        row = self._table.get_row_at(selected)
        if not row:
            return
        
        agent_id = row[0]
        
        if event.button.id == "start-agent":
            self._start_agent(agent_id)
        elif event.button.id == "stop-agent":
            self._stop_agent(agent_id)
        elif event.button.id == "refresh-agents":
            self._load_agents()
    
    @work
    async def _start_agent(self, agent_id: str) -> None:
        """Start an agent."""
        if self.gateway and self.gateway.agent_manager:
            await self.gateway.agent_manager.start_agent(agent_id)
            self._load_agents()
    
    @work
    async def _stop_agent(self, agent_id: str) -> None:
        """Stop an agent."""
        if self.gateway and self.gateway.agent_manager:
            await self.gateway.agent_manager.stop_agent(agent_id)
            self._load_agents()


class SessionsScreen(Screen):
    """Session management screen."""
    
    def __init__(self, gateway=None, app=None):
        super().__init__()
        self.gateway = gateway
        self.app_ref = app
    
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "delete_session", "Delete"),
        Binding("escape", "go_back", "Back"),
    ]
    
    def compose(self) -> ComposeResult:
        """Compose the sessions screen."""
        yield Header()
        
        with Vertical(id="sessions-container"):
            yield Static("[b]Session Management[/b]", id="sessions-title")
            
            # Session table
            yield DataTable(id="sessions-table")
            
            # Stats
            yield Static("", id="session-stats")
            
            # Actions
            with Horizontal(id="session-actions"):
                yield Button("Refresh", id="refresh-sessions")
                yield Button("Delete", id="delete-session", variant="error")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self._table = self.query_one("#sessions-table", DataTable)
        self._stats = self.query_one("#session-stats", Static)
        self._table.add_columns("ID", "Agent", "Channel", "User", "Messages", "Created", "Status")
        self._load_sessions()
    
    def _load_sessions(self) -> None:
        """Load sessions from gateway."""
        self._table.clear()
        
        if self.gateway and self.gateway.session_manager:
            sessions = self.gateway.session_manager.list_sessions()
            
            for session in sessions:
                status = "Active" if session.is_active else "Inactive"
                created = session.created_at.strftime("%H:%M:%S")
                self._table.add_row(
                    session.id[:8] + "...",
                    session.agent_id,
                    session.channel,
                    session.user_id,
                    str(session.message_count),
                    created,
                    status,
                )
            
            # Update stats
            total = len(sessions)
            active = len([s for s in sessions if s.is_active])
            self._stats.update(f"Total: {total} | Active: {active}")
    
    def action_refresh(self) -> None:
        """Refresh session list."""
        self._load_sessions()
    
    def action_delete_session(self) -> None:
        """Delete selected session."""
        selected = self._table.cursor_row
        if selected is None:
            return
        
        row = self._table.get_row_at(selected)
        if row:
            session_id = row[0].replace("...", "")
            # Find full session ID
            if self.gateway and self.gateway.session_manager:
                sessions = self.gateway.session_manager.list_sessions()
                for s in sessions:
                    if s.id.startswith(session_id):
                        session_id = s.id
                        break
                
                asyncio.create_task(
                    self.gateway.session_manager.delete_session(session_id)
                )
                self._load_sessions()
    
    def action_go_back(self) -> None:
        """Go back to previous screen."""
        self.app_ref.pop_screen()
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "refresh-sessions":
            self._load_sessions()
        elif event.button.id == "delete-session":
            self.action_delete_session()


class StatusScreen(Screen):
    """Gateway status dashboard."""
    
    def __init__(self, gateway=None, app=None):
        super().__init__()
        self.gateway = gateway
        self.app_ref = app
    
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "go_back", "Back"),
    ]
    
    def compose(self) -> ComposeResult:
        """Compose the status screen."""
        yield Header()
        
        with ScrollableContainer(id="status-container"):
            yield Static("[b]Gateway Status[/b]\n", id="gateway-status")
            yield Static("[b]Agents Status[/b]\n", id="agents-status")
            yield Static("[b]Session Status[/b]\n", id="session-status")
            yield Static("[b]Config[/b]\n", id="config-status")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self._gateway_status = self.query_one("#gateway-status", Static)
        self._agents_status = self.query_one("#agents-status", Static)
        self._session_status = self.query_one("#session-status", Static)
        self._config_status = self.query_one("#config-status", Static)
        self._load_status()
    
    def _load_status(self) -> None:
        """Load status from gateway."""
        # Gateway status
        if self.gateway:
            self._gateway_status.update(
                f"[b]Gateway Status[/b]\n"
                f"Version: {self.gateway.config.version if self.gateway.config else 'N/A'}\n"
                f"Running: {self.gateway._is_running if hasattr(self.gateway, '_is_running') else 'Unknown'}"
            )
        else:
            self._gateway_status.update(
                "[b]Gateway Status[/b]\nGateway not initialized"
            )
        
        # Agents status
        if self.gateway and self.gateway.agent_manager:
            status = self.gateway.agent_manager.get_status()
            agents_text = f"[b]Agents Status[/b]\n"
            agents_text += f"Total: {status['total_agents']} | Running: {status['running_agents']}\n"
            for agent in status.get('agents', []):
                agents_text += f"  - {agent['name']}: {'Running' if agent['is_running'] else 'Stopped'}\n"
            self._agents_status.update(agents_text)
        else:
            self._agents_status.update(
                "[b]Agents Status[/b]\nNo agent manager"
            )
        
        # Session status
        if self.gateway and self.gateway.session_manager:
            status = self.gateway.session_manager.get_status()
            sessions_text = f"[b]Session Status[/b]\n"
            sessions_text += f"Total: {status['total_sessions']} | Active: {status['active_sessions']}\n"
            sessions_text += f"Messages: {status['total_messages']} | Users: {status['unique_users']}\n"
            sessions_text += f"Channels: {', '.join(status.get('channels', []))}"
            self._session_status.update(sessions_text)
        else:
            self._session_status.update(
                "[b]Session Status[/b]\nNo session manager"
            )
        
        # Config status
        if self.gateway and self.gateway.config:
            cfg = self.gateway.config
            config_text = f"[b]Config[/b]\n"
            config_text += f"Config Version: {cfg.version}\n"
            config_text += f"Debug: {getattr(cfg, 'debug', False)}\n"
            config_text += f"Log Level: {getattr(cfg, 'log_level', 'INFO')}"
            self._config_status.update(config_text)
        else:
            self._config_status.update(
                "[b]Config[/b]\nNo config loaded"
            )
    
    def action_refresh(self) -> None:
        """Refresh status."""
        self._load_status()
    
    def action_go_back(self) -> None:
        """Go back to previous screen."""
        self.app_ref.pop_screen()


class LogsScreen(Screen):
    """Real-time log viewer."""
    
    BINDINGS = [
        Binding("c", "clear_logs", "Clear"),
        Binding("r", "toggle_auto_scroll", "Auto-scroll"),
        Binding("escape", "go_back", "Back"),
    ]
    
    def __init__(self, gateway=None, app=None):
        super().__init__()
        self.gateway = gateway
        self.app_ref = app
        self._auto_scroll = True
    
    def compose(self) -> ComposeResult:
        """Compose the logs screen."""
        yield Header()
        
        with Vertical(id="logs-container"):
            # Log controls
            with Horizontal(id="log-controls"):
                yield Switch("Auto-scroll", id="auto-scroll-switch", value=True)
                yield Button("Clear", id="clear-logs")
            
            # Log viewer
            yield Log(id="log-viewer", highlight=True)
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when screen is mounted."""
        self._log_viewer = self.query_one("#log-viewer", Log)
        self._auto_scroll_switch = self.query_one("#auto-scroll-switch", Switch)
        
        # Write welcome message
        self._log_viewer.write("[bold]PyClaw Log Viewer[/bold]")
        self._log_viewer.write("Press 'c' to clear, 'r' to toggle auto-scroll")
    
    def action_clear_logs(self) -> None:
        """Clear the log viewer."""
        self._log_viewer.clear()
    
    def action_toggle_auto_scroll(self) -> None:
        """Toggle auto-scroll."""
        self._auto_scroll = not self._auto_scroll
        self._auto_scroll_switch.value = self._auto_scroll
    
    def action_go_back(self) -> None:
        """Go back to previous screen."""
        self.app_ref.pop_screen()
    
    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Handle switch change."""
        if event.switch.id == "auto-scroll-switch":
            self._auto_scroll = event.value
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "clear-logs":
            self.action_clear_logs()
    
    def write_log(self, message: str) -> None:
        """Write a message to the log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log_viewer.write(f"[{timestamp}] {message}")
    
    def write_error(self, message: str) -> None:
        """Write an error message."""
        self.write_log(f"[red]ERROR:[/red] {message}")
    
    def write_warning(self, message: str) -> None:
        """Write a warning message."""
        self.write_log(f"[yellow]WARN:[/yellow] {message}")


# Widget classes

class AgentListWidget(Static):
    """Widget to display list of agents."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._agents: Dict[str, Dict[str, Any]] = {}
    
    def compose(self) -> ComposeResult:
        """Compose the widget."""
        yield Static("Agents", id="agent-list-title")
    
    def add_agent(self, agent_id: str, name: str, is_running: bool) -> None:
        """Add an agent to the list."""
        self._agents[agent_id] = {
            "name": name,
            "is_running": is_running,
        }
        self._update_display()
    
    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the list."""
        self._agents.pop(agent_id, None)
        self._update_display()
    
    def update_agent_status(self, agent_id: str, is_running: bool) -> None:
        """Update agent status."""
        if agent_id in self._agents:
            self._agents[agent_id]["is_running"] = is_running
            self._update_display()
    
    def _update_display(self) -> None:
        """Update the display."""
        lines = ["[b]Agents[/b]", ""]
        for agent_id, info in self._agents.items():
            status = "🟢" if info["is_running"] else "🔴"
            lines.append(f"{status} {info['name']}")
        
        self.update("\n".join(lines))
