"""Action button component for the TUI."""

from typing import Callable, Optional

from textual.widgets import Button
from textual.app import ComposeResult
from textual.events import Click


class ActionButton(Button):
    """A styled action button with callback support."""
    
    def __init__(
        self,
        label: str = "",
        action_id: str = "",
        variant: str = "default",
        callback: Optional[Callable] = None,
        *args,
        **kwargs,
    ):
        super().__init__(label, *args, **kwargs)
        self.action_id = action_id
        self.variant = variant
        self.callback = callback
        
        # Apply variant
        if variant != "default":
            self.variant = variant
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if self.callback:
            self.callback()


class QuickActionBar(Static):
    """A bar containing multiple action buttons."""
    
    DEFAULT_CSS = """
    QuickActionBar {
        height: auto;
        layout: horizontal;
    }
    
    QuickActionBar Button {
        margin: 0 1;
    }
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buttons: list[ActionButton] = []
    
    def compose(self) -> ComposeResult:
        """Compose the action bar."""
        with self.container:
            pass
    
    @property
    def container(self):
        """Get the button container."""
        return self
    
    def add_action(
        self,
        label: str,
        action_id: str = "",
        variant: str = "default",
        callback: Optional[Callable] = None,
    ) -> ActionButton:
        """Add an action button."""
        button = ActionButton(
            label=label,
            action_id=action_id,
            variant=variant,
            callback=callback,
        )
        self._buttons.append(button)
        self.mount(button)
        return button
    
    def remove_action(self, action_id: str) -> None:
        """Remove an action button by ID."""
        for button in self._buttons:
            if button.action_id == action_id:
                button.remove()
                self._buttons.remove(button)
                break
    
    def clear_actions(self) -> None:
        """Clear all action buttons."""
        for button in self._buttons:
            button.remove()
        self._buttons.clear()
    
    def get_actions(self) -> list[ActionButton]:
        """Get all action buttons."""
        return self._buttons.copy()
