# pyclaw Overview

pyclaw is a modular AI agent gateway. It connects LLM agents (backed by
FastAgent) to messaging channels (Telegram, Slack, TUI, HTTP), tools (via MCP),
memory, scheduled jobs, and an extensible hook system. All subsystems are wired
together by the **Gateway**.

---

## Request Flow

```
Telegram / Slack / TUI / HTTP API
    → Gateway (pyclaw/core/gateway.py)
        → deduplication (_seen_message_ids TTL map)
        → allowlist/denylist check (per-channel SecurityConfig)
        → SessionManager — find or create active session
        → CommandRegistry — handle /slash commands
            → returns None for unknown commands (fall through to agent)
        → SessionMessageQueue — debounce/batch rapid messages
        → Agent (pyclaw/core/agent.py)
            → AgentRunner (pyclaw/agents/runner.py) — wraps FastAgent
                → FastAgent connects to pyclaw MCP server (port 8081)
                    → tools call REST API (port 8080) for jobs/memory/config
        → reply streamed back to originating channel
```

---

## Startup Sequence

**Order matters.** FastAgent eagerly connects to MCP servers during agent
initialization. Both the MCP server (8081) and REST API (8080) must be up
before `gateway.initialize()` is called.

```
1. load config
2. start pyclaw MCP server  (FastMCP on 8081) ← FastAgent connects to this; includes self-knowledge tools
3. start REST API server    (FastAPI  on 8080) ← MCP tools call this
5. gateway.initialize()
   → create AgentManager (builds Agent + AgentRunner per configured agent)
   → create SessionManager, JobScheduler, HookRegistry, etc.
   → fire gateway:startup hook
6. start Telegram polling tasks (one asyncio.Task per bot)
7. run until Ctrl+C
8. gateway.stop()
   → stop agents (close FA MCP connections)
   → stop MCP server
   → stop API server
```

---

## Three Servers, One Process

pyclaw runs three HTTP servers in the same asyncio event loop:

| Server | Port | Library | Purpose |
|--------|------|---------|---------|
| pyclaw MCP | 8081 | FastMCP | Tool server for FastAgent (incl. self-knowledge tools) |
| REST API | 8080 | FastAPI/uvicorn | External clients + MCP tool callbacks |

FastMCP owns its own uvicorn lifecycle for the MCP servers. The REST API
uvicorn is managed directly by the Gateway.

---

## Key Files

| File | Role |
|------|------|
| `pyclaw/core/gateway.py` | Main orchestrator: channels, sessions, agents, jobs, server lifecycle |
| `pyclaw/core/agent.py` | Agent dataclass + AgentManager; session runner cache |
| `pyclaw/agents/runner.py` | AgentRunner wraps FastAgent; `run_stream()` yields `(text, is_reasoning)` |
| `pyclaw/core/session.py` | Session (metadata only) + SessionManager; reaper; active_session pointer |
| `pyclaw/core/commands.py` | CommandRegistry: `/help /reset /status /model /job /skills /skill` |
| `pyclaw/core/queue.py` | SessionMessageQueue with 7 modes |
| `pyclaw/tools/server.py` | FastMCP server exposing all built-in tools (port 8081) |
| `pyclaw/self/server.py` | FastMCP self-knowledge server (port 8082) |
| `pyclaw/api/app.py` | FastAPI REST API (port 8080) |
| `pyclaw/jobs/scheduler.py` | Cron/interval/one-shot scheduler with notify_callback |
| `pyclaw/config/schema.py` | Pydantic config schema — all fields use validation_alias for camelCase |
| `pyclaw/config/loader.py` | Loads ~/.pyclaw/config.yaml; resolves ${source:id} inline secrets |
| `pyclaw/__main__.py` | CLI entry point; startup orchestration |

---

## Data Directories

```
~/.pyclaw/
├── config.yaml               ← main config
├── logs/
│   └── pyclaw.log            ← gateway log (daily rotation)
├── agents/
│   └── {agent_id}/
│       ├── active_session    ← pointer file (plain text session ID)
│       ├── logs/agent.log    ← per-agent log
│       ├── sessions/
│       │   └── {YYYY-MM-DD}-{6chars}/
│       │       ├── session.json          ← routing metadata
│       │       ├── history.json          ← FA PromptMessageExtended
│       │       ├── history_previous.json ← rotation backup
│       │       └── archived/             ← files moved here by /reset
│       ├── jobs.yaml         ← scheduled jobs for this agent
│       ├── runs/             ← JSONL run logs per job
│       └── memory/
│           ├── MEMORY.md     ← curated, injected into sessions
│           └── YYYY-MM-DD.md ← daily memory journal
├── skills/                   ← user-installed global skills
└── todos.json                ← todo store
```

---

## Config File Location

pyclaw looks for config in order:
1. `--config` CLI flag
2. `~/.pyclaw/config.yaml`
3. `./config.yaml` (current directory)

See `development/conventions` for config schema patterns.
See `systems/config` for the full config schema reference.
