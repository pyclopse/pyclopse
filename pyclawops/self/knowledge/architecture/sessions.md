# Session System

pyclawops uses a two-layer session model. The **routing layer** (pyclawops's `Session` + `SessionManager`) handles who is talking to which agent and on which channel. The **history layer** delegates conversation storage entirely to FastAgent's native serialisation format, living on disk per-agent under `~/.pyclawops/agents/`.

---

## Design Principles

- **One active session per agent.** Each agent has exactly one live session at any time, pointed to by an `active_session` pointer file. Channels (Telegram, Slack, TUI, HTTP) are just transport — they all converge on the same agent session. There is no "session per channel" or "session per user".
- **Channels are routing metadata, not session keys.** When a message arrives via Telegram, pyclawops looks up the agent's active session and updates `last_channel`/`last_user_id`/`last_thread_ts` on it so replies know where to go. The session itself is not tied to any particular channel.
- **Session = routing metadata only.** `Session` carries no message content. It knows who, where, and when — not what was said.
- **FastAgent owns the history.** Conversation messages are stored in FastAgent's `PromptMessageExtended` JSON format. The `AgentRunner` reads and writes those files directly using FA's serialisation helpers.
- **Files are never deleted.** The reaper and delete APIs remove sessions from the in-memory index; the session directory and its history files remain on disk indefinitely.
- **Crash safety.** History is only written on successful turn completion (the `_completed` flag). A ctrl+c or async cancellation mid-turn leaves the previous complete state intact.

---

## Directory Layout

```
~/.pyclawops/agents/
└── {agent_id}/
    ├── active_session              ← pointer: contains the active session ID (plain text)
    └── sessions/
        └── {YYYY-MM-DD}-{6chars}/ ← session directory
            ├── session.json        ← routing metadata
            ├── history.json        ← current conversation (FA-native JSON)
            ├── history_previous.json ← previous rotation backup
            └── archived/           ← history files moved here by /reset
                ├── history.json.20260311_142300
                └── history_previous.json.20260311_142300
```

The session ID encodes the creation date: `2026-03-11-aB3xYz`. The 6-character suffix is generated with `secrets.choice` over `[A-Za-z0-9]`.

The `active_session` file is written atomically (via a `.tmp` rename) by `SessionManager.set_active_session()`.

---

## Session Metadata (`session.json`)

This file holds all routing and state information. It contains no message content.

```json
{
  "id": "2026-03-11-aB3xYz",
  "agent_id": "assistant",
  "channel": "telegram",
  "user_id": "123456789",
  "last_channel": "slack",
  "last_user_id": "U123456",
  "last_thread_ts": "1710000000.123456",
  "created_at": "2026-03-11T14:00:00",
  "updated_at": "2026-03-11T14:23:00",
  "message_count": 8,
  "is_active": true,
  "metadata": {},
  "context": {
    "model_override": "opus"
  }
}
```

Key fields:

| Field | Purpose |
|---|---|
| `id` | Date-prefixed unique identifier |
| `agent_id` | Which agent handles this session |
| `channel` | Channel at session creation time (historical; see `last_channel`) |
| `user_id` | User at session creation time (historical; see `last_user_id`) |
| `last_channel` | Channel of the most recent inbound message — used for reply routing |
| `last_user_id` | User of the most recent inbound message — used for reply routing |
| `last_thread_ts` | Slack thread timestamp for the most recent message (Slack threading only) |
| `message_count` | Incrementing count of user+assistant turns (2 per exchange) |
| `context` | Runtime overrides — e.g. `model_override` set by `/model` |
| `metadata` | Arbitrary bag for channel plugins and hooks |

`last_channel` / `last_user_id` / `last_thread_ts` are updated on every inbound message across all channels. The gateway's `_deliver_to_session()` reads these to route replies back correctly.

The `context` dict is persisted so overrides like `/model gpt-4o` survive gateway restarts. Keys starting with `_` are excluded from serialisation.

---

## History File (`history.json`)

This is a FastAgent `PromptMessageExtended` JSON file — the same format FA uses natively:

```json
{
  "messages": [
    {
      "role": "user",
      "content": [{"type": "text", "text": "What time is it?"}]
    },
    {
      "role": "assistant",
      "content": [{"type": "text", "text": "It is 2:00 PM."}],
      "stop_reason": "end_turn"
    }
  ]
}
```

`content` is a list of content blocks. For plain text conversations this will always be a single `TextContent`. Tool calls, tool results, images, and embedded resources are represented as additional block types — the format preserves all FA fields (`stop_reason`, `tool_calls`, `is_template`, etc.).

The file is written using `fast_agent.mcp.prompt_serialization.save_messages()` and read back with `load_messages()`.

### Error message stripping

Before saving, `AgentRunner._save_history()` strips any trailing FastAgent internal error messages (strings starting with `"I hit an internal error"`) from `agent.message_history`, both on disk and in-memory. This prevents a model API failure during one turn from poisoning all future turns. If an error response is detected during `run()`, a `RuntimeError` is raised immediately (preventing the save entirely). On any exception, `agent.handle_message()` evicts the session runner so the next message gets a fresh FA context.

### Rotation

Before writing a new `history.json`, the runner rotates the previous file:

```
.history.tmp.{random}.json   ← FA writes here first
history.json                 → history_previous.json   ← atomically renamed
.history.tmp.json            → history.json            ← atomically renamed
```

Using `os.replace()` (POSIX `rename`) makes each step atomic. On any crash during writing, the reader always has either the complete current file or the complete previous file. A partial write stays in `.history.tmp.*` and is cleaned up on the next successful write.

---

## AgentRunner: the bridge

`AgentRunner` (`pyclawops/agents/runner.py`) wraps FastAgent and is responsible for all history I/O. Each session gets its own `AgentRunner` instance, cached in `agent._session_runners[session_id]`.

### Construction

```python
runner = AgentRunner(
    agent_name="assistant-2026-03-11",
    instruction=system_prompt,
    model="sonnet",
    history_path=session.history_path,  # ~/.pyclawops/agents/.../history.json
    ...
)
```

`history_path` is set from `session.history_path` (a `@property` on `Session` that returns `history_dir / "history.json"`). If `None`, no I/O occurs (used for isolated job sessions where `context["no_history"] = True`).

### First turn — loading history

On the first `run()` or `run_stream()` call, `_load_history()` is invoked once:

```python
async def _load_history(self) -> None:
    if self._history_loaded:          # guard: runs at most once per runner lifetime
        return
    self._history_loaded = True
    if self.history_path is None or not self.history_path.exists():
        return
    messages = load_messages(str(self.history_path))
    agent = self._app._agent(None)
    agent.load_message_history(messages)
```

This injects the persisted `PromptMessageExtended` list directly into FA's internal message history. From FA's perspective the agent has always had this history.

### Every successful turn — saving history

After each complete non-streaming turn:

```python
_completed = False
try:
    async with get_manager().acquire(self.model):
        result = await self._app.send(prompt)
    response = str(result)
    if response.startswith("I hit an internal error"):
        raise RuntimeError(response)   # don't save; evict runner
    ...
    _completed = True
    return response
finally:
    if _completed:
        await self._save_history()
```

The `_completed` flag is the crash-safety mechanism. `_save_history()` only executes if the turn completed without error. A ctrl+c, `asyncio.CancelledError`, or FA error leaves the previous good state intact.

---

## Session Lifecycle

### Active session pointer

Each agent has one active session at a time, tracked by a pointer file:

```
~/.pyclawops/agents/{agent_id}/active_session   ← contains session ID, e.g. "2026-03-11-aB3xYz"
```

`SessionManager` provides two operations:

```python
# Read the active session (returns None if no pointer exists or session not found)
session = await session_manager.get_active_session(agent_id)

# Write the pointer (atomic rename via .tmp)
session_manager.set_active_session(agent_id, session_id)
```

### Message arrival (all channels)

```
inbound message arrives (Telegram / Slack / TUI / HTTP)
    → gateway calls _get_active_session(agent_id, channel, user_id)
        → session_manager.get_active_session(agent_id)
            → if no pointer or session not found: create new session + set pointer
        → session.last_channel  = channel
        → session.last_user_id  = user_id
        → session.last_thread_ts = thread_ts  (Slack only)
        → session.save_metadata()
    → gateway calls agent.handle_message(message, session)
    → runner created (first time) or retrieved from cache
    → runner._load_history() injects FA history (first call only)
    → runner.run() or run_stream()
    → on completion: runner._save_history() writes FA history to disk
    → session.touch(count_delta=2) updates message_count + writes session.json
```

### Reply routing

After the agent responds, the gateway delivers the reply back via `_deliver_to_session(session, text)`:

```python
channel   = session.last_channel or session.channel
user_id   = session.last_user_id or session.user_id
thread_ts = session.last_thread_ts   # Slack only
```

This means:
- A Telegram message → switches `last_channel` to `"telegram"` → next reply goes to Telegram
- A Slack message → switches `last_channel` to `"slack"` → next reply goes to Slack
- A job result delivered with `report_to_agent` → delivered via whatever channel is currently active

### Daily rollover

At midnight, `get_active_session()` detects that the current session's date doesn't match today and triggers `_archive_and_rollover()`:

```
current session → archived (history files moved to archived/)
new session created for today → set as active_session
last_channel / last_user_id / last_thread_ts preserved on new session
```

The rollover fires the `SESSION_CREATED` hook for the new session.

### In-memory index

`SessionManager.sessions` is a dict of all live sessions. On gateway startup, `_load_sessions_from_disk()` scans all `session.json` files under `agents_dir` and rebuilds this index.

### Reaper

Two background tasks run while the gateway is up:

| Task | Period | Action |
|---|---|---|
| `_cleanup_loop` | Every 60 s | Marks sessions as `is_active=False` if idle > `session_timeout` (default 1 h); removes from index |
| `_reaper_loop` | Every `reaper_interval_minutes` (default 60) | Removes sessions from index if `updated_at < now - ttl_hours`; fires optional `on_expire` callback |

**Neither task deletes files or the `active_session` pointer.** They only remove entries from the in-memory dicts. Session directories and history files remain on disk permanently.

### /reset command

`/reset` creates a fresh session and updates the active pointer:

```
/reset received
    → history.json          → archived/history.json.YYYYMMDD_HHMMSS
    → history_previous.json → archived/history_previous.json.YYYYMMDD_HHMMSS
    → new session created (same agent_id, channel, user_id)
    → new session.last_channel/last_user_id/last_thread_ts populated
    → set_active_session(agent_id, new_session.id)
    → agent.evict_session_runner(old_session.id)
        → runner.cleanup() closes FA MCP connections
        → removes runner from _session_runners cache
```

The next message gets a fresh `AgentRunner` with no history loaded. The archived files preserve the old conversation permanently.

### Runner eviction on error

If `agent.handle_message()` throws (e.g. a FA model error, task group expiry), it calls `agent.evict_session_runner(session.id)` before returning the error `OutgoingMessage`. A new runner is created for the next message, which will reload history from disk — giving a clean reconnect while preserving context.

---

## Job Sessions (special case)

Jobs use a separate path that bypasses the active session system:

```python
if channel == "job":
    session = await _get_or_create_session(agent_id, "job", session_user_id)
```

Job sessions never become the agent's `active_session`. They use `context["no_history"] = True` for isolated runs (the default), so no history is loaded or saved. After the job completes, the runner is evicted immediately.

If a job has `report_to_agent` set, the result is delivered via the named agent's current active session and channel:

```python
target_session = await session_manager.get_active_session(report_to_agent)
await _deliver_to_session(target_session, f"📋 Job *{job.name}*:\n{response}")
```

---

## FastAgent Integration Points

pyclawops uses FastAgent strictly as an LLM execution engine. It does **not** use FA's own session management.

| FA component | How pyclawops uses it |
|---|---|
| `FastAgent` / `fast.run()` | Creates the FA app context, initialises MCP connections |
| `agent.send(prompt)` | Non-streaming turn execution |
| `agent.add_stream_listener()` | Streaming chunk delivery |
| `agent.load_message_history(messages)` | Injects prior history at runner startup |
| `agent.message_history` | Reads current full history for saving |
| `load_messages(path)` | Deserialises `PromptMessageExtended` from `history.json` |
| `save_messages(messages, path)` | Serialises `PromptMessageExtended` to `history.json` |

---

## Config

`SessionsConfig` in `pyclawops/config/schema.py`:

```yaml
sessions:
  ttlHours: 24              # how long before idle sessions are reaped from index
  reaperIntervalMinutes: 60 # how often the reaper runs
  maxSessions: 1000         # max sessions in the in-memory index
  sessionTimeout: 3600      # seconds idle before is_active → false
  dailyRollover: true       # create a new session at midnight and archive the old one
```

The session directory is always `~/.pyclawops/agents/{agent_id}/sessions/` — not configurable.

---

## Importing OpenClaw History

pyclawops can import session history from OpenClaw (the TypeScript gateway pyclawops is inspired by). OpenClaw stores sessions as JSONL files:

```
~/.openclaw/agents/{agent}/sessions/{session_id}.jsonl
```

Each line is a typed JSON record (`type: "session"`, `type: "message"`, `type: "compaction"`).

### Running the import

```bash
pyclawops import-openclaw --all                        # all agents
pyclawops import-openclaw --agent myagent              # one agent
pyclawops import-openclaw --all --openclaw-dir ~/backup/openclaw
```

The importer (`pyclawops/tools/openclaw_import.py`):

1. Reads each `.jsonl` file and extracts `type: "message"` records with `role: user|assistant`.
2. Converts each to a `PromptMessageExtended` with a single `TextContent` block.
3. Serialises the list to FA JSON format using `to_json()`.
4. Derives the session date from the `updatedAt` or `createdAt` field in the session record.
5. Writes to `~/.pyclawops/agents/{agent}/sessions/{YYYY-MM-DD}-{6chars}/history.json`.
6. Writes `session.json` with `metadata.imported_from = "openclaw"` and `channel = "openclaw"`.

Imported sessions appear in the TUI session list on the next gateway start. Their history is fully searchable and resumable. They do **not** become the active session automatically.

---

## API Access

`GET /api/v1/sessions/{id}` returns session metadata plus message history loaded from `history.json`:

```json
{
  "id": "2026-03-11-aB3xYz",
  "agent_id": "assistant",
  "channel": "telegram",
  "last_channel": "slack",
  "last_user_id": "U123456",
  "message_count": 8,
  "messages": [
    {"id": "2026-03-11-aB3xYz-0", "role": "user",      "content": "...", "timestamp": "..."},
    {"id": "2026-03-11-aB3xYz-1", "role": "assistant",  "content": "...", "timestamp": "..."}
  ]
}
```

`GET /api/v1/sessions/` supports filtering by `agent_id`, `channel`, `user_id`, and `active_only` (default `true`).

`DELETE /api/v1/sessions/{id}` removes the session from the in-memory index. Files are not deleted.

---

## Testing

Session tests use `agents_dir=str(tmp_path)` — never `persist_dir=`.

```python
# SessionManager in tests
mgr = SessionManager(agents_dir=str(tmp_path), ttl_hours=1)
await mgr.start()
session = await mgr.create_session("agent1", "telegram", "user1")
mgr.set_active_session("agent1", session.id)

# Retrieve active session
active = await mgr.get_active_session("agent1")
assert active.id == session.id

# Gateway stubs need the active session mock
mock_sm = MagicMock()
mock_sm.get_active_session = AsyncMock(return_value=mock_session)
mock_sm.create_session     = AsyncMock(return_value=mock_session)
mock_sm.set_active_session = MagicMock()
gw._session_manager = mock_sm
```

For runner tests that involve history I/O, patch `fast_agent.mcp.prompt_serialization.load_messages` and `save_messages`. The `_completed` flag means `_save_history()` is only called after a full successful `run()`.
