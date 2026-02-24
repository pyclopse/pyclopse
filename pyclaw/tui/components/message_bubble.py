"""Message bubble component for chat display."""

from datetime import datetime
from typing import Optional

from textual.widgets import Static
from textual.app import ComposeResult


class MessageBubble(Static):
    """A chat message bubble with styling based on role."""
    
    DEFAULT_CSS = """
    MessageBubble {
        height: auto;
        padding: 1 2;
        margin: 1 0;
        border: solid $border;
        border-radius: 3;
    }
    
    MessageBubble.user {
        background: $accent;
        border-color: $accent-dark;
    }
    
    MessageBubble.assistant {
        background: $panel;
        border-color: $border;
    }
    
    MessageBubble.system {
        background: $warning;
        border-color: $warning-dark;
    }
    """
    
    def __init__(
        self,
        content: str = "",
        role: str = "user",
        timestamp: Optional[datetime] = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.content = content
        self.role = role
        self.timestamp = timestamp or datetime.now()
    
    def compose(self) -> ComposeResult:
        """Compose the message bubble."""
        timestamp_str = self.timestamp.strftime("%H:%M")
        
        role_colors = {
            "user": "blue",
            "assistant": "green",
            "system": "yellow",
        }
        color = role_colors.get(self.role, "white")
        
        role_label = {
            "user": "You",
            "assistant": "Bot",
            "system": "System",
        }
        label = role_label.get(self.role, self.role.title())
        
        yield Static(
            f"[bold {color}]{label}[/bold {color}] {timestamp_str}\n{self.content}",
            id="bubble-content",
        )
    
    def update_content(self, content: str) -> None:
        """Update the message content."""
        self.content = content
        content_elem = self.query_one("#bubble-content", Static)
        
        role_colors = {
            "user": "blue",
            "assistant": "green",
            "system": "yellow",
        }
        color = role_colors.get(self.role, "white")
        
        role_label = {
            "user": "You",
            "assistant": "Bot",
            "system": "System",
        }
        label = role_label.get(self.role, self.role.title())
        
        content_elem.update(
            f"[bold {color}]{label}[/bold {color}] {self.timestamp.strftime('%H:%M')}\n{self.content}"
        )
