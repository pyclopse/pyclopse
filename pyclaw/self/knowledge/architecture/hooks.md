# Hook System

**Files:** `pyclaw/hooks/registry.py`, `pyclaw/hooks/events.py`,
`pyclaw/hooks/loader.py`, `pyclaw/hooks/bundled/`

The hook system is pyclaw's event-driven extension mechanism. It has two
distinct handler patterns: **notification** (all fire, return ignored) and
**intercept** (first non-None wins, with fallback).

---

## Handler Patterns

### Notification

All registered handlers fire. Return values are ignored. Exceptions are caught
and logged — a failing handler never breaks the caller.

Used for side-effects: logging, auditing, session-memory saves, boot injection.

```python
await hook_registry.notify(HookEvent.MESSAGE_RECEIVED, payload)
```

### Intercept

Handlers fire in priority order (lowest number = highest priority). The first
handler returning a non-`None` value wins; remaining handlers are skipped. If
all handlers return `None`, the default implementation runs.

Used for `memory:*` operations so plugins can transparently swap the backend.

```python
result = await hook_registry.intercept(
    HookEvent.MEMORY_READ, payload, default=file_backend.read
)
```

---

## Hook Events (`pyclaw/hooks/events.py`)

| Event | Pattern | Fired when |
|-------|---------|------------|
| `gateway:startup` | notify | Gateway finishes initialising |
| `gateway:shutdown` | notify | Gateway is stopping |
| `message:received` | notify | Inbound message before agent |
| `message:sent` | notify | Outbound reply after agent |
| `command:reset` | notify | `/reset` slash command executes |
| `command:*` | notify | Any slash command |
| `session:created` | notify | New session first message |
| `session:expired` | notify | Reaper evicts idle session |
| `agent:after_response` | notify | Agent finishes a turn |
| `tool:before_exec` | notify | Before tool call |
| `tool:after_exec` | notify | After tool call |
| `heartbeat:tick` | notify | Pulse runner fires |
| `memory:read` | intercept | Memory read operation |
| `memory:write` | intercept | Memory write operation |
| `memory:delete` | intercept | Memory delete operation |
| `memory:search` | intercept | Memory search operation |
| `memory:list` | intercept | Memory list operation |

---

## HookRegistry (`pyclaw/hooks/registry.py`)

```python
registry = HookRegistry()

# Register a handler
registry.register(
    event=HookEvent.MESSAGE_RECEIVED,
    handler=my_async_handler,  # async callable
    priority=50,               # lower = higher priority
    description="My handler",
    source="code",             # "code" or "file"
)

# Fire notification
await registry.notify(HookEvent.MESSAGE_RECEIVED, payload)

# Fire intercept with default fallback
result = await registry.intercept(HookEvent.MEMORY_READ, payload, default=fallback_fn)
```

`HookRegistration` dataclass: `event`, `handler`, `priority`, `description`,
`source` ("code" for Python handlers, "file" for subprocess handlers).

---

## Bundled Hooks (`pyclaw/hooks/bundled/`)

### `session-memory`

**Event:** `command:reset`

When `/reset` is called, writes the current session's conversation history to
the agent's memory before the session is archived. Preserves conversation
context across resets.

Handler: `pyclaw/hooks/bundled/session-memory/handler.py`

### `boot-md`

**Event:** `gateway:startup`

At startup, injects the agent's `MEMORY.md` content into the agent's context
via a REST API call. This ensures the curated memory file is available from
the very first message, before any memory tools are called.

Handler: `pyclaw/hooks/bundled/boot-md/handler.py`

---

## Hook Loader (`pyclaw/hooks/loader.py`)

Reads `hooks.bundled` and `hooks.custom` from config. Supports two handler types:

**Python handlers** — async functions defined in config or loaded from modules.

**File handlers** — Python scripts run as subprocesses. The loader wraps them
in an async shim that executes the script and waits for its output. Useful for
hooks that need isolation or are written in other languages.

---

## Adding a Custom Hook

In `pyclaw.yaml`:

```yaml
hooks:
  custom:
    - event: message:received
      handler: /path/to/my_hook.py
      priority: 10
      description: "My custom handler"
```

Or register programmatically before `gateway.initialize()`:

```python
gateway._hook_registry.register(
    HookEvent.MESSAGE_RECEIVED,
    my_handler,
    priority=10,
    description="Programmatic handler",
)
```

See `development/extending` for the full extension guide.
