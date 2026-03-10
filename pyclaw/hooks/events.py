"""Hook event name constants."""


class HookEvent:
    """
    Named hook events fired throughout the pyclaw lifecycle.

    Two categories:

    Notification events — all registered handlers run; return values are
    ignored.  Use these for logging, auditing, side-effects, etc.

    Interceptable events — handlers run in priority order and the first
    handler that returns a non-None value short-circuits the chain.  The
    returned value is used as the operation result instead of the default
    backend.  The memory:* events use this mechanism so that a plugin can
    transparently replace ClawVault with an alternative backend.
    """

    # ------------------------------------------------------------------ #
    # Gateway lifecycle
    # ------------------------------------------------------------------ #
    GATEWAY_STARTUP = "gateway:startup"
    GATEWAY_SHUTDOWN = "gateway:shutdown"

    # ------------------------------------------------------------------ #
    # Message flow
    # ------------------------------------------------------------------ #
    MESSAGE_RECEIVED = "message:received"   # inbound, before agent
    MESSAGE_SENT = "message:sent"           # outbound, after agent reply

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    COMMAND_NEW = "command:new"
    COMMAND_RESET = "command:reset"
    COMMAND_ANY = "command:*"               # wildcard — fires for every command

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    SESSION_CREATED = "session:created"
    SESSION_EXPIRED = "session:expired"

    # ------------------------------------------------------------------ #
    # Agent
    # ------------------------------------------------------------------ #
    AGENT_RESPONSE = "agent:after_response"

    # ------------------------------------------------------------------ #
    # Tool execution
    # ------------------------------------------------------------------ #
    TOOL_BEFORE = "tool:before_exec"
    TOOL_AFTER = "tool:after_exec"

    # ------------------------------------------------------------------ #
    # Heartbeat / cron
    # ------------------------------------------------------------------ #
    HEARTBEAT_TICK = "heartbeat:tick"

    # ------------------------------------------------------------------ #
    # Memory (interceptable)
    #
    # Plugins that want to replace ClawVault register handlers for these
    # events and return the operation result.  If no plugin handles an
    # event the default ClawVault backend is used.
    # ------------------------------------------------------------------ #
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    MEMORY_DELETE = "memory:delete"
    MEMORY_SEARCH = "memory:search"
    MEMORY_LIST = "memory:list"

    # Frozen set of events that use the intercept (first-wins) contract
    INTERCEPTABLE: frozenset = frozenset({
        MEMORY_READ,
        MEMORY_WRITE,
        MEMORY_DELETE,
        MEMORY_SEARCH,
        MEMORY_LIST,
    })
