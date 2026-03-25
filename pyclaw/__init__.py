"""pyclaw — modular AI agent gateway.

pyclaw connects LLM agents (backed by FastAgent) to messaging channels
(Telegram, Slack, TUI, HTTP), tools (via MCP), memory, scheduled jobs,
and an extensible hook system.  All subsystems are wired together by the
**Gateway**.

## Request Flow

```
Telegram / Slack / TUI / HTTP API
    → Gateway (pyclaw/core/gateway.py)
        → deduplication (_seen_message_ids TTL map)
        → allowlist/denylist check (per-channel SecurityConfig)
        → SessionManager — find or create active session
        → CommandRegistry — handle /slash commands
        → SessionMessageQueue — debounce/batch rapid messages
        → Agent (pyclaw/core/agent.py)
            → AgentRunner (pyclaw/agents/runner.py) — wraps FastAgent
                → FastAgent connects to pyclaw MCP server (port 8081)
                    → tools call REST API (port 8080) for jobs/memory/config
        → reply streamed back to originating channel
```

## Startup Sequence

FastAgent eagerly connects to MCP servers during agent initialization.
Both servers must be up before `gateway.initialize()` is called.

```
1. load config
2. start pyclaw MCP server  (FastMCP on 8081) ← FastAgent connects here
3. start REST API server    (FastAPI  on 8080) ← MCP tools call this
4. gateway.initialize()
   → AgentManager (one Agent + AgentRunner per configured agent)
   → SessionManager, JobScheduler, HookRegistry, QueueManager
   → fire gateway:startup hook
5. start Telegram / Slack polling tasks
6. run until Ctrl+C → gateway.stop()
```

## Two Servers, One Process

| Server     | Port | Library        | Purpose                               |
|------------|------|----------------|---------------------------------------|
| pyclaw MCP | 8081 | FastMCP        | Tool server for FastAgent             |
| REST API   | 8080 | FastAPI/uvicorn | External clients + MCP tool callbacks |

FastMCP owns its uvicorn lifecycle.  The REST API uvicorn is managed
directly by the Gateway.

## Key Systems

| System       | File(s)                              | Role                                      |
|--------------|--------------------------------------|-------------------------------------------|
| gateway      | pyclaw/core/gateway.py               | Main orchestrator                         |
| agents       | pyclaw/core/agent.py                 | Agent + AgentManager                      |
| agent-runner | pyclaw/agents/runner.py              | FastAgent wrapper; streaming              |
| sessions     | pyclaw/core/session.py               | Metadata-only sessions + reaper           |
| commands     | pyclaw/core/commands.py              | /slash command dispatcher                 |
| queue        | pyclaw/core/queue.py                 | Per-session message queue (7 modes)       |
| jobs         | pyclaw/jobs/scheduler.py             | Cron/interval/one-shot job scheduler      |
| hooks        | pyclaw/hooks/                        | Event/hook registry + bundled hooks       |
| memory       | pyclaw/memory/                       | ClawVault + MemoryService routing         |
| skills       | pyclaw/skills/                       | Skill discovery + injection               |
| channels     | pyclaw/channels/                     | Channel plugin system (Telegram, Slack)   |
| security     | pyclaw/security/                     | Exec approval + audit logging             |
| config       | pyclaw/config/                       | Pydantic schema + YAML loader             |
| mcp-server   | pyclaw/tools/server.py               | FastMCP tool server (port 8081)           |
| a2a          | pyclaw/agents/a2a.py                 | Agent-to-Agent protocol endpoints         |
| tui          | pyclaw/tui/                          | Textual TUI dashboard                     |
| concurrency  | pyclaw/core/concurrency.py           | Per-model asyncio semaphore throttling    |
| reflection   | pyclaw/reflect/                      | Live reflection of pyclaw architecture    |

## Data Directories

```
~/.pyclaw/
├── config.yaml               ← main config
├── logs/pyclaw.log            ← gateway log (daily rotation)
├── agents/{agent_id}/
│   ├── active_session         ← pointer file (plain-text session ID)
│   ├── sessions/{YYYY-MM-DD}-{6chars}/
│   │   ├── session.json       ← routing metadata
│   │   ├── history.json       ← FA PromptMessageExtended
│   │   └── archived/          ← files moved here by /reset
│   ├── jobs.yaml              ← scheduled jobs for this agent
│   ├── runs/                  ← JSONL run logs per job
│   └── memory/MEMORY.md       ← curated, injected into sessions
├── skills/                    ← user-installed global skills
└── todos.json                 ← todo store
```

## Reflection

Use the ``reflect`` MCP tool to explore pyclaw's architecture live:

```
reflect()                        → this overview
reflect(category="system")       → list all registered systems
reflect(category="system", name="gateway")   → gateway detail
reflect(category="command")      → list all slash commands
reflect(category="event")        → list all hook events
reflect(category="config", name="agents")    → agents config schema
reflect_source(module="gateway") → raw source with line numbers
```
"""

try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0.dev0"
__author__ = "pyclaw team"

from .config import load_config, Config, ConfigLoader

__all__ = [
    "__version__",
    "load_config",
    "Config",
    "ConfigLoader",
]
