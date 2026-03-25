"""Hook event name constants."""

from pyclawops.reflect import reflect_event


@reflect_event("hook-events")
class HookEvent:
    """Named hook event constants fired throughout the pyclawops lifecycle.

    This class acts as a namespace of string constants — it is never
    instantiated. Importing ``HookEvent`` is the only required import for
    event name lookups.

    Two categories of events are defined:

    **Notification events** — all registered handlers are called in priority
    order. Return values are ignored. Use these for logging, auditing, and
    side-effects.

    **Interceptable events** — handlers run in priority order and the first
    handler that returns a non-None value short-circuits the chain. The
    returned value replaces the default backend result. The ``memory:*``
    events use this contract so that plugins can transparently substitute an
    alternative memory backend in place of FileMemoryBackend.

    Attributes:
        GATEWAY_STARTUP (str): Fired once when the gateway has fully started up.
        GATEWAY_SHUTDOWN (str): Fired once when the gateway is shutting down.
        MESSAGE_RECEIVED (str): Inbound message, fired before any preprocessing.
        MESSAGE_TRANSCRIBED (str): Fired after voice/audio transcription (voice only).
        MESSAGE_PREPROCESSED (str): After preprocessing, before agent dispatch.
        MESSAGE_SENT (str): Outbound reply, fired after the agent response is sent.
        COMMAND_NEW (str): Fired when a /new slash command is processed.
        COMMAND_RESET (str): Fired when a /reset slash command is processed.
        COMMAND_ANY (str): Wildcard — also fires for every command:xxx event.
        SESSION_CREATED (str): Fired when a new session is created.
        SESSION_EXPIRED (str): Fired when a session is evicted by the TTL reaper.
        AGENT_BOOTSTRAP (str): New session runner created; bootstrap files loaded.
        AGENT_RESPONSE (str): Fired after an agent produces a response.
        TOOL_BEFORE (str): Fired before a tool is executed.
        TOOL_AFTER (str): Fired after a tool execution completes.
        MEMORY_READ (str): Interceptable — read a memory entry by key.
        MEMORY_WRITE (str): Interceptable — write a memory entry.
        MEMORY_DELETE (str): Interceptable — delete a memory entry.
        MEMORY_SEARCH (str): Interceptable — search memory for matching entries.
        MEMORY_LIST (str): Interceptable — list all memory entries.
        INTERCEPTABLE (frozenset): Set of event names that use the first-wins contract.
    """

    # ------------------------------------------------------------------ #
    # Gateway lifecycle
    # ------------------------------------------------------------------ #
    GATEWAY_STARTUP = "gateway:startup"
    GATEWAY_SHUTDOWN = "gateway:shutdown"

    # ------------------------------------------------------------------ #
    # Message flow
    # ------------------------------------------------------------------ #
    MESSAGE_RECEIVED = "message:received"       # inbound, before any preprocessing
    MESSAGE_TRANSCRIBED = "message:transcribed" # after voice/audio transcription (voice input only)
    MESSAGE_PREPROCESSED = "message:preprocessed"  # after preprocessing, before agent dispatch
    MESSAGE_SENT = "message:sent"               # outbound, after agent reply

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
    AGENT_BOOTSTRAP = "agent:bootstrap"         # new session runner created; bootstrap files loaded
    AGENT_RESPONSE = "agent:after_response"

    # ------------------------------------------------------------------ #
    # Tool execution
    # ------------------------------------------------------------------ #
    TOOL_BEFORE = "tool:before_exec"
    TOOL_AFTER = "tool:after_exec"

    # ------------------------------------------------------------------ #
    # Memory (interceptable)
    #
    # Plugins that want to replace the default FileMemoryBackend register
    # handlers for these events and return the operation result.  If no
    # plugin handles an event the default FileMemoryBackend is used.
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
