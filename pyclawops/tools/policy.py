"""Tool policy engine - per-agent tool allowlist/denylist with group support."""
from enum import Enum
from typing import Dict, List, Optional, Set


class ToolGroup(str, Enum):
    """Named tool groups that mirror OpenClaw's tool-catalog groups.

    Each member maps to a logical category of MCP tools.  The special
    ``ALL`` member expands to the union of every other group.

    Attributes:
        ALL (str): All tools from every group combined.
        FS (str): Filesystem tools — read_file, write_file, edit_file, list_directory, create_directory.
        EXEC (str): Execution tools — bash, process_status, process_kill.
        WEB (str): Web tools — web_fetch, web_search.
        MEMORY (str): Memory tools — memory_search, memory_get.
        MESSAGING (str): Messaging tools — send_message.
        SESSIONS (str): Session tools — sessions_list, sessions_history, sessions_send.
        TIME (str): Time tools — get_current_time, convert_timezone.
        SYSTEM (str): System tools — session_status.
    """
    ALL = "all"
    FS = "fs"              # read_file, write_file, edit_file, list_directory, create_directory
    EXEC = "exec"          # bash, process_status, process_kill
    WEB = "web"            # web_fetch, web_search
    MEMORY = "memory"      # memory_search, memory_get
    MESSAGING = "messaging"  # send_message
    SESSIONS = "sessions"  # sessions_list, sessions_history, sessions_send
    TIME = "time"          # get_current_time, convert_timezone
    SYSTEM = "system"      # session_status


# Map groups to their member tools
TOOL_GROUPS: Dict[str, Set[str]] = {
    ToolGroup.ALL: set(),  # populated below
    ToolGroup.FS: {"read_file", "write_file", "edit_file", "list_directory", "create_directory"},
    ToolGroup.EXEC: {"bash", "process_status", "process_kill"},
    ToolGroup.WEB: {"web_fetch", "web_search"},
    ToolGroup.MEMORY: {"memory_search", "memory_get"},
    ToolGroup.MESSAGING: {"send_message"},
    ToolGroup.SESSIONS: {"sessions_list", "sessions_history", "sessions_send"},
    ToolGroup.TIME: {"get_current_time", "convert_timezone"},
    ToolGroup.SYSTEM: {"session_status"},
}
# ALL = union of everything
TOOL_GROUPS[ToolGroup.ALL] = {t for g in TOOL_GROUPS.values() for t in g}

# Named profiles (mirrors OpenClaw's tool-catalog profiles)
TOOL_PROFILES: Dict[str, Set[str]] = {
    "minimal": TOOL_GROUPS[ToolGroup.SYSTEM],
    "coding": (
        TOOL_GROUPS[ToolGroup.FS]
        | TOOL_GROUPS[ToolGroup.EXEC]
        | TOOL_GROUPS[ToolGroup.MEMORY]
        | TOOL_GROUPS[ToolGroup.SESSIONS]
        | TOOL_GROUPS[ToolGroup.SYSTEM]
    ),
    "web": (
        TOOL_GROUPS[ToolGroup.WEB]
        | TOOL_GROUPS[ToolGroup.FS]
        | TOOL_GROUPS[ToolGroup.TIME]
        | TOOL_GROUPS[ToolGroup.SYSTEM]
    ),
    "messaging": (
        TOOL_GROUPS[ToolGroup.MESSAGING]
        | TOOL_GROUPS[ToolGroup.SESSIONS]
        | TOOL_GROUPS[ToolGroup.SYSTEM]
    ),
    "full": TOOL_GROUPS[ToolGroup.ALL],
}


class ToolPolicy:
    """Resolves which tools an agent may use based on profile, allowlist, and denylist.

    Config examples (in agent YAML):

    .. code-block:: yaml

        tools:
          profile: coding          # named profile
          allow: [web_search]      # additional tools on top of profile
          deny: [bash]             # remove tools from profile

        tools:
          allow: [bash, read_file, web_search]  # explicit allowlist

        tools:
          profile: full
          deny: [bash]             # full minus bash

    Attributes:
        _config (Dict): Raw tools configuration dict from the agent YAML.
        _allowed (Optional[Set[str]]): Lazily computed set of allowed tool names.
    """

    def __init__(self, tools_config: Optional[Dict] = None):
        """Initialize the tool policy from a raw configuration dict.

        Args:
            tools_config (Optional[Dict]): Tool policy configuration with optional
                keys ``profile``, ``allow``, and ``deny``. Defaults to an empty dict
                (full access).
        """
        self._config = tools_config or {}
        self._allowed: Optional[Set[str]] = None

    @property
    def allowed(self) -> Set[str]:
        """Return the resolved set of allowed tool names (lazily computed).

        Returns:
            Set[str]: Set of tool names this policy permits.
        """
        if self._allowed is None:
            self._allowed = self._resolve()
        return self._allowed

    def _resolve(self) -> Set[str]:
        """Compute the allowed tool set from profile, allow, and deny config.

        Starts from the named profile's tool set (or an empty set for explicit
        allowlists, or ``ALL`` if no profile and no allow list).  Then expands
        ``group:name`` prefixes and bare group names in the allow/deny lists
        before adding and subtracting from the base set.

        Returns:
            Set[str]: Resolved set of allowed tool names.
        """
        cfg = self._config

        # Start from profile if specified
        profile = cfg.get("profile")
        if profile:
            base = set(TOOL_PROFILES.get(profile, TOOL_PROFILES["full"]))
        elif cfg.get("allow"):
            base = set()
        else:
            # Default: full access
            base = set(TOOL_GROUPS[ToolGroup.ALL])

        # Expand group names in allow/deny lists
        def expand(names: List[str]) -> Set[str]:
            """Expand a list of tool/group names into a flat set of tool names.

            Args:
                names (List[str]): Tool or group names; supports ``group:name``
                    prefix syntax and bare group names (e.g. ``"fs"``).

            Returns:
                Set[str]: Flat set of individual tool names.
            """
            result: Set[str] = set()
            for name in names:
                if name.startswith("group:"):
                    key = name[6:]
                    result |= TOOL_GROUPS.get(key, set())
                elif name in TOOL_GROUPS:
                    result |= TOOL_GROUPS[name]
                else:
                    result.add(name)
            return result

        # Add explicit allows
        base |= expand(cfg.get("allow", []))
        # Remove explicit denies
        base -= expand(cfg.get("deny", []))

        return base

    def is_allowed(self, tool_name: str) -> bool:
        """Check whether a specific tool is permitted by this policy.

        Args:
            tool_name (str): The MCP tool name to check.

        Returns:
            bool: True if the tool is in the allowed set, False otherwise.
        """
        return tool_name in self.allowed

    def filter_tools(self, tools: List[str]) -> List[str]:
        """Return only tools from the given list that this policy permits.

        Args:
            tools (List[str]): List of tool names to filter.

        Returns:
            List[str]: Subset of tools that are allowed by this policy.
        """
        return [t for t in tools if self.is_allowed(t)]
