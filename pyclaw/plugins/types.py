"""Plugin type definitions and metadata."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyclaw.core.gateway import Gateway


class PluginType(str, Enum):
    """Types of plugins supported by pyclaw."""
    PYTHON = "python"       # Native Python plugin (loaded directly)
    HTTP = "http"           # HTTP/RPC plugin (separate process)
    SUBPROCESS = "subprocess"  # stdio communication (any language)
    JSON = "json"           # Config-only plugins (no code)
    CHANNEL = "channel"     # Channel adapter plugins
    SKILL = "skill"         # Skill plugins
    TOOL = "tool"           # Tool plugins
    MIDDLEWARE = "middleware"  # Middleware plugins
    STORAGE = "storage"     # Storage backend plugins


class PluginState(str, Enum):
    """Lifecycle states of a plugin."""
    DISCOVERED = "discovered"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class PluginMetadata:
    """Metadata for a plugin."""
    name: str
    version: str
    description: str = ""
    author: str = ""
    plugin_type: PluginType = PluginType.CHANNEL
    dependencies: List[str] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if isinstance(self.plugin_type, str):
            self.plugin_type = PluginType(self.plugin_type)


@dataclass
class PluginInfo:
    """Complete information about a loaded plugin."""
    metadata: PluginMetadata
    instance: Optional['Plugin'] = None
    state: PluginState = PluginState.DISCOVERED
    error: Optional[str] = None
    loaded_at: Optional[datetime] = None
    config: Dict[str, Any] = field(default_factory=dict)
