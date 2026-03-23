# Gateway

**File:** `pyclaw/core/gateway.py`

The Gateway is the central orchestrator. It owns every other subsystem, manages
the async lifecycle, routes inbound messages from channels through agents, and
fires hook events at each stage.

---

## Responsibilities

- Bootstrap: load config, instantiate all subsystems
- Start/stop the MCP server (FastMCP), self server, and REST API (FastAPI)
- Register and dispatch to channel adapters (Telegram, Slack, plugins)
- Inbound message deduplication (`_seen_message_ids` with TTL)
- Per-channel allowlist/denylist enforcement
- Route messages through CommandRegistry → SessionMessageQueue → Agent
- Stream responses back to channels
- Track usage counters (`_usage = {"messages": ..., "tokens": ...}`)
- Manage the JobScheduler lifecycle
- Fire hook events at key lifecycle points

---

## Lifecycle

```python
gateway = Gateway(config_path)

# Must start before gateway.initialize()
await gateway.start_mcp_server(host, mcp_port)
await gateway.start_self_server(host, self_port)
await gateway.start_api_server(host, api_port)

await gateway.initialize()   # creates agents, sessions, jobs, hooks

# ... run until stopped ...

await gateway.stop()         # reverse order: agents → mcp → api
```

`initialize()` creates: AgentManager, SessionManager, JobScheduler, HookRegistry,
AuditLogger, ExecApprovalSystem, and all channel adapters. It fires the
`gateway:startup` hook at the end.

`stop()` cleans up in reverse order: stops all agents (closes FA MCP connections),
cancels polling tasks, stops the MCP server, stops the API server.

---

## Message Dispatch

`gateway.dispatch(channel, user_id, user_name, text, message_id?)` is the single
entry point for all inbound messages regardless of channel.

Steps:
1. **Deduplication** — check `_seen_message_ids[message_id]`; skip duplicates
2. **Security** — check allowlist/denylist for the channel
3. **Session** — `_get_active_session(agent_id, channel, user_id)` via SessionManager
4. **Command check** — `CommandRegistry.dispatch(text, context)` for `/slash`
5. **Queue** — enqueue into `SessionMessageQueue` for the session
6. **Agent** — dequeue and call `agent.handle_message(message, session)`
7. **Reply** — stream response back via `_deliver_to_session(session, text)`

---

## Deduplication

`_seen_message_ids: dict[str, float]` maps message IDs to insertion timestamps.
TTL default: 60 seconds. Checked on every inbound message; prevents double
delivery when Telegram/Slack re-deliver due to network issues.

---

## Multi-Bot Telegram

`_tg_bots: dict[str, Bot]` — one Telegram Bot per configured bot token.
Each bot gets its own polling `asyncio.Task` (named `telegram-poll-{bot_name}`).
All bots dispatch into the same gateway via `dispatch()`.

---

## MCP Server Lifecycle

`start_mcp_server()` and `stop_mcp_server()` manage a single asyncio Task that
runs `pyclaw_mcp.run_http_async()`. FastMCP owns the uvicorn lifecycle internally
— do NOT replace this with direct uvicorn calls.

On stop, noisy uvicorn/starlette loggers are silenced to CRITICAL before
cancelling the task, then restored. This suppresses expected shutdown noise.

---

## Important Attributes

| Attribute | Type | Purpose |
|-----------|------|---------|
| `config` | Config | Loaded pyclaw config |
| `_agent_manager` | AgentManager | All configured agents |
| `_session_manager` | SessionManager | Session index + reaper |
| `_job_scheduler` | JobScheduler | Cron/interval/one-shot jobs |
| `_hook_registry` | HookRegistry | All registered hooks |
| `_audit` | AuditLogger | Audit log writer |
| `_approval_system` | ExecApprovalSystem | Bash tool policy |
| `_tg_bots` | dict | Telegram Bot instances |
| `_tg_polling_tasks` | dict | Polling tasks per bot |
| `_mcp_server_task` | Task | FastMCP server task |
| `_self_server_task` | Task | Self MCP server task |
| `_usage` | dict | `{"messages": int, "tokens": int}` |
| `_is_running` | bool | Set True after initialize() |

---

## Testing Pattern

Gateway is never instantiated normally in unit tests. Use the stub pattern:

```python
gw = Gateway.__new__(Gateway)
gw._seen_message_ids = {}
gw._dedup_ttl_seconds = 60
gw._usage = {"messages": 0, "tokens": 0}
# then attach mocks as needed
gw._session_manager = mock_session_manager
gw._agent_manager = mock_agent_manager
```

`Gateway.__new__` skips `__init__` entirely — no config loading, no server
startup. This is the correct pattern for all unit tests that need a Gateway.
