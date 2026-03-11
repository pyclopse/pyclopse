# Session System

pyclaw uses a two-layer session model. The **routing layer** (pyclaw's `Session` + `SessionManager`) handles who is talking to which agent and on which channel. The **history layer** delegates conversation storage entirely to FastAgent's native serialisation format, living on disk per-agent under `~/.pyclaw/agents/`.

---

## Design Principles

- **Session = routing metadata only.** `Session` carries no message content. It knows who, where, and when — not what was said.
- **FastAgent owns the history.** Conversation messages are stored in FastAgent's `PromptMessageExtended` JSON format. The `AgentRunner` reads and writes those files directly using FA's serialisation helpers.
- **Files are never deleted.** The reaper and delete APIs remove sessions from the in-memory index; the session directory and its history files remain on disk indefinitely. This is intentional — history is kept for future indexing and search.
- **Crash safety.** History is only written on successful turn completion (the `_completed` flag). A ctrl+c or async cancellation mid-turn leaves the previous complete state intact.

---

## Directory Layout

Every agent has its own sessions directory:

```
~/.pyclaw/agents/
└── {agent_id}/
    └── sessions/
        └── {YYYY-MM-DD}-{6chars}/      ← session directory
            ├── session.json            ← routing metadata
            ├── history.json            ← current conversation (FA-native JSON)
            ├── history_previous.json   ← previous rotation backup
            └── archived/               ← history files moved here by /reset
                ├── history.json.20260311_142300
                └── history_previous.json.20260311_142300
```

The session ID encodes the creation date: `2026-03-11-aB3xYz`. The 6-character suffix is generated with `secrets.choice` over `[A-Za-z0-9]`.

---

## Session Metadata (`session.json`)

This file holds all routing and state information. It contains no message content.

```json
{
  "id": "2026-03-11-aB3xYz",
  "agent_id": "assistant",
  "channel": "telegram",
  "user_id": "123456789",
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
| `channel` | Source channel (`telegram`, `slack`, `http`, `tui`) |
| `user_id` | Channel-specific user identifier |
| `message_count` | Incrementing count of user+assistant turns (2 per exchange) |
| `context` | Runtime overrides — e.g. `model_override` set by `/model` |
| `metadata` | Arbitrary bag for channel plugins and hooks |

The `context` dict is persisted so overrides like `/model gpt-4o` survive gateway restarts. Keys starting with `_` are excluded from serialisation (they hold non-serialisable runtime objects like runner references).

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

The file is written using `fast_agent.mcp.prompt_serialization.save_messages()` and read back with `load_messages()`. Both functions auto-select JSON format based on the `.json` extension.

### Rotation

Before writing a new `history.json`, the runner rotates the previous file:

```
.history.tmp.{random}.json   ← FA writes here first
history.json                 → history_previous.json   ← atomically renamed
.history.tmp.json            → history.json            ← atomically renamed
```

Using `os.replace()` (POSIX `rename`) makes each step atomic. On any crash during writing, the reader always has either the complete current file, the complete previous file, or both. A partial write stays in `.history.tmp.*` and is cleaned up on the next successful write.

---

## AgentRunner: the bridge

`AgentRunner` (`pyclaw/agents/runner.py`) wraps FastAgent and is responsible for all history I/O. Each session gets its own `AgentRunner` instance, cached in `agent._session_runners[session_id]`.

### Construction

```python
runner = AgentRunner(
    agent_name="assistant-2026-03-11",
    instruction=system_prompt,
    model="sonnet",
    history_path=session.history_path,  # ~/.pyclaw/agents/.../history.json
    ...
)
```

`history_path` is set from `session.history_path` (a `@property` on `Session` that returns `history_dir / "history.json"`). If the session has no `history_dir` (e.g. in-memory-only sessions during testing), `history_path` is `None` and no I/O occurs.

### First turn — loading history

On the first `run()` or `run_stream()` call, `_load_history()` is invoked:

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

This injects the persisted `PromptMessageExtended` list directly into FA's internal message history. From FA's perspective the agent has always had this history — it appears as natural prior context, not injected prompts.

### Every successful turn — saving history

After each complete non-streaming turn:

```python
_completed = False
try:
    async with get_manager().acquire(self.model):
        result = await self._app.send(prompt)
    response = str(result)
    ...
    _completed = True
    return response
finally:
    if _completed:
        await self._save_history()
```

The `_completed` flag is the crash-safety mechanism. `finally` always runs, but `_save_history()` only executes if the turn completed without error or cancellation. A ctrl+c or `asyncio.CancelledError` mid-generation sets `_completed = False`, skipping the save. The previous good state in `history.json` is left untouched.

Streaming turns follow the same pattern:

```python
_completed = False
try:
    async with get_manager().acquire(self.model):
        async for item in self._run_stream_inner(prompt):
            yield item
    _completed = True
finally:
    if _completed:
        await self._save_history()
```

Both `run()` and `run_stream()` save the full FA message history (including tool calls and results, not just the visible text). The saved messages come directly from `agent.message_history` — whatever FA has accumulated, including intermediate tool exchange turns.

---

## Session Lifecycle

### Creation

```
inbound message arrives
    → gateway calls session_manager.get_or_create_session(agent_id, channel, user_id)
    → SessionManager generates ID: "2026-03-11-aB3xYz"
    → creates Session(history_dir=~/.pyclaw/agents/assistant/sessions/2026-03-11-aB3xYz/)
    → writes session.json immediately (atomic)
    → returns session to gateway
```

Session directories are created lazily on first write: `session.save_metadata()` calls `history_dir.mkdir(parents=True, exist_ok=True)`.

### Active use

On each message turn:

```
message arrives
    → gateway gets/creates session
    → gateway calls agent._get_session_runner(session.id, history_path=session.history_path)
    → runner created (first time) or retrieved from cache
    → runner._load_history() injects FA history (first call only)
    → runner.run(prompt) or runner.run_stream(prompt)
    → on completion: runner._save_history() writes FA history to disk
    → session.touch(count_delta=2) updates metadata + writes session.json
```

### In-memory index

`SessionManager.sessions` is a dict of all live sessions. On gateway startup, `_load_sessions_from_disk()` scans all `session.json` files under `agents_dir` and rebuilds this index. The index also maintains two lookup maps:

- `user_sessions[user_id]` → list of session IDs
- `channel_sessions[channel]` → list of session IDs

`get_or_create_session()` uses `channel_sessions` to find the most recent active session for a `(channel, user_id)` pair. If none exists, a new session is created.

### Reaper

Two background tasks run while the gateway is up:

| Task | Period | Action |
|---|---|---|
| `_cleanup_loop` | Every 60 s | Marks sessions as `is_active=False` if idle > `session_timeout` (default 1 h); removes from index |
| `_reaper_loop` | Every `reaper_interval_minutes` (default 60) | Removes sessions from index if `updated_at < now - ttl_hours`; fires optional `on_expire` callback |

**Neither task deletes files.** They only remove entries from the in-memory dicts. Session directories and history files remain on disk permanently, making them available for future indexing or search.

### /reset command

`/reset` archives history without deleting it, then forces a fresh FastAgent context:

```
/reset received
    → history.json         → archived/history.json.YYYYMMDD_HHMMSS
    → history_previous.json → archived/history_previous.json.YYYYMMDD_HHMMSS
    → session.message_count = 0
    → session.save_metadata()
    → agent.evict_session_runner(session.id)
        → runner.cleanup() closes FA MCP connections
        → removes runner from _session_runners cache
```

The next message creates a fresh `AgentRunner` with no history loaded (the `history.json` is gone). The archived files preserve the conversation permanently.

### Runner eviction on error

If an FA turn throws (e.g. a task group expiry during startup races), gateway calls `agent.evict_session_runner(session.id)`. A new runner is created for the retry, which will reload history from disk — giving a clean reconnect while preserving context.

---

## FastAgent Integration Points

pyclaw uses FastAgent strictly as an LLM execution engine. It does **not** use FA's own `SessionManager` for session lifecycle — that would impose FA's 20-session window pruning and its own directory layout. Instead, pyclaw takes the minimal necessary surface from FA:

| FA component | How pyclaw uses it |
|---|---|
| `FastAgent` / `fast.run()` | Creates the FA app context, initialises MCP connections |
| `agent.send(prompt)` | Non-streaming turn execution |
| `agent.add_stream_listener()` | Streaming chunk delivery |
| `agent.load_message_history(messages)` | Injects prior history at runner startup |
| `agent.message_history` | Reads current full history for saving |
| `load_messages(path)` | Deserialises `PromptMessageExtended` from `history.json` |
| `save_messages(messages, path)` | Serialises `PromptMessageExtended` to `history.json` |

The `PromptMessageExtended` format is used throughout because it is lossless: tool calls, tool results, stop reasons, and multi-part content are all preserved. This is what makes history meaningful beyond simple text — a reloaded session truly resumes the full prior state including any tool exchange turns.

---

## Config

`SessionsConfig` in `pyclaw/config/schema.py` exposes:

```yaml
sessions:
  ttlHours: 24              # how long before idle sessions are reaped from index
  reaperIntervalMinutes: 60 # how often the reaper runs
  maxSessions: 1000         # max sessions in the in-memory index
  sessionTimeout: 3600      # seconds idle before is_active → false
```

The session directory is always `~/.pyclaw/agents/{agent_id}/sessions/` — it is not configurable. This keeps the layout consistent with agent memory, skills, and other per-agent data.

---

## Importing OpenClaw History

pyclaw can import session history from OpenClaw (the TypeScript gateway pyclaw is inspired by). OpenClaw stores sessions as JSONL files:

```
~/.openclaw/agents/{agent}/sessions/{session_id}.jsonl
```

Each line is a typed JSON record (`type: "session"`, `type: "message"`, `type: "compaction"`).

### Running the import

```bash
pyclaw import-openclaw --all                        # all agents
pyclaw import-openclaw --agent myagent              # one agent
pyclaw import-openclaw --all --openclaw-dir ~/backup/openclaw
```

The importer (`pyclaw/tools/openclaw_import.py`):

1. Reads each `.jsonl` file and extracts `type: "message"` records with `role: user|assistant`.
2. Converts each to a `PromptMessageExtended` with a single `TextContent` block.
3. Serialises the list to FA JSON format using `to_json()`.
4. Derives the session date from the `updatedAt` or `createdAt` field in the session record.
5. Writes to `~/.pyclaw/agents/{agent}/sessions/{YYYY-MM-DD}-{6chars}/history.json`.
6. Writes `session.json` with `metadata.imported_from = "openclaw"` and `channel = "openclaw"`.

Imported sessions appear in the TUI session list on the next gateway start (they are loaded from disk by `_load_sessions_from_disk()`). Their history is fully searchable and resumable.

---

## API Access

`GET /api/v1/sessions/{id}` returns session metadata plus message history loaded from `history.json`:

```json
{
  "id": "2026-03-11-aB3xYz",
  "agent_id": "assistant",
  "channel": "telegram",
  "message_count": 8,
  "messages": [
    {"id": "2026-03-11-aB3xYz-0", "role": "user",      "content": "...", "timestamp": "..."},
    {"id": "2026-03-11-aB3xYz-1", "role": "assistant",  "content": "...", "timestamp": "..."}
  ]
}
```

Message content is extracted via `PromptMessageExtended.all_text()` — multi-part content blocks are joined into a single string. Tool-call turns are included as assistant messages with their text representation.

`GET /api/v1/sessions/` supports filtering by `agent_id`, `channel`, `user_id`, and `active_only` (default `true`).

`DELETE /api/v1/sessions/{id}` removes the session from the in-memory index. Files are not deleted.

---

## Testing

Session tests use `agents_dir=str(tmp_path)` — never `persist_dir=`. Constructing a `Session` directly requires `history_dir` to be set if you want persistence:

```python
# Real Session with persistence
hist_dir = tmp_path / "sessions" / "test-session"
session = Session(id="test-session", agent_id="a", channel="tg",
                  user_id="u", history_dir=hist_dir)
session.touch(count_delta=2)  # writes session.json

# SessionManager in tests
mgr = SessionManager(agents_dir=str(tmp_path), ttl_hours=1)
await mgr.start()
session = await mgr.create_session("agent1", "telegram", "user1")
assert session.history_dir.parent.name == "sessions"
```

For runner tests that involve history I/O, patch `fast_agent.mcp.prompt_serialization.load_messages` and `save_messages`. The `_completed` flag means save is only called after a full successful `run()` — cancelled or errored calls never write.
