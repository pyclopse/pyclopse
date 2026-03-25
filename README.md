# PyClawOps

**PyClawOps** is a modular AI agent gateway written in Python. It connects one or more LLM agents — powered by [FastAgent](https://github.com/evalstate/fast-agent) — to messaging channels (Telegram, Slack, TUI, HTTP), tools (via MCP), long-term memory, scheduled jobs, and an extensible hook system. All subsystems are wired together by a central **Gateway** process.

pyclawops is inspired by [OpenClaw](https://github.com/jondecker76/openclaw) but is a ground-up Python rewrite with its own architecture, idioms, and feature set. See [pyclawops vs OpenClaw](#pyclawops-vs-openclaw) for a detailed comparison.

---

## Table of Contents

- [Quick Start](#quick-start)
- [pyclawops vs OpenClaw](#pyclawops-vs-openclaw)
- [Configuration Reference](#configuration-reference)
  - [providers](#providers)
  - [agents](#agents)
  - [channels](#channels)
  - [gateway](#gateway)
  - [memory](#memory)
  - [security](#security)
  - [sessions](#sessions)
  - [jobs](#jobs)
- [Systems Reference](#systems-reference)
  - [Gateway](#gateway-system)
  - [Agents & AgentRunner](#agents--agentrunner)
  - [Sessions](#sessions-system)
  - [Message Queue](#message-queue)
  - [Commands](#commands)
  - [Jobs & Subagents](#jobs--subagents)
  - [Hooks](#hooks)
  - [Memory & Vault](#memory--vault)
  - [Skills](#skills)
  - [Channels](#channels-system)
  - [Security](#security-system)
  - [MCP Server](#mcp-server)
  - [A2A Protocol](#a2a-protocol)
  - [Reflection](#reflection)
  - [TUI Dashboard](#tui-dashboard)

---

## Quick Start

> **Note:** The deployment process is currently being finalised. This section will be updated once a distribution strategy is decided. The steps below reflect the development workflow.

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- At least one LLM provider API key (Anthropic, OpenAI, MiniMax, etc.)

### Install (Development)

```bash
git clone git@github.com:jondecker76/pyclawops.git
cd pyclawops
uv sync
```

### Configure

Create `~/.pyclawops/config.yaml`. A minimal working config:

```yaml
version: "1.0"

providers:
  anthropic:
    apiKey: "${env:ANTHROPIC_API_KEY}"
    models:
      claude-sonnet-4-6:
        enabled: true
        concurrency: 2

agents:
  main:
    name: Main
    model: anthropic/claude-sonnet-4-6
    contextWindow: 200000
    use_fastagent: true
    mcp_servers:
      - pyclawops
      - fetch
      - time
      - filesystem

gateway:
  host: 0.0.0.0
  port: 8080
  log_level: info
```

### Run

```bash
# With TUI dashboard (default)
uv run python -m pyclawops run

# Headless (no TUI)
uv run python -m pyclawops run --headless

# With a specific config file
uv run python -m pyclawops run --config /path/to/pyclawops.yaml

# Validate config only
uv run python -m pyclawops validate
```

### Data Directory

pyclawops stores all runtime data under `~/.pyclawops/`:

```
~/.pyclawops/
├── config.yaml                    ← main config
├── logs/pyclawops.log                ← gateway log (daily rotation)
├── agents/{agent_id}/
│   ├── active_session             ← pointer to current session ID
│   ├── sessions/{YYYY-MM-DD}-{6}/
│   │   ├── session.json           ← routing metadata
│   │   ├── history.json           ← conversation history
│   │   └── archived/              ← history files moved here by /reset
│   ├── jobs.yaml                  ← scheduled jobs
│   ├── runs/                      ← job run logs (JSONL)
│   └── memory/
│       ├── MEMORY.md              ← curated context, injected at startup
│       └── YYYY-MM-DD.md          ← daily memory journal
├── skills/                        ← user-installed global skills
└── todos.json                     ← todo store
```

---

## pyclawops vs OpenClaw

pyclawops is **not** a port of OpenClaw. It shares design philosophy and some terminology but differs substantially in implementation:

| Area | OpenClaw | pyclawops |
|------|----------|--------|
| **Language** | TypeScript (Node.js) | Python (asyncio) |
| **LLM layer** | Anthropic SDK directly | FastAgent (abstracted, supports any provider) |
| **Multi-provider** | Anthropic only | Anthropic, OpenAI, MiniMax, Google, and any OpenAI-compatible endpoint via `generic` provider |
| **MCP** | mcp SDK | FastMCP (server) + FastAgent (client); never manages uvicorn directly |
| **Session history** | JSONL per session | FastAgent-native `PromptMessageExtended` JSON; loaded lazily into FA context |
| **Active session model** | Per-channel session | One active session per agent; channels are routing metadata only |
| **Message queue** | Basic | 7-mode queue (`followup`, `collect`, `interrupt`, `steer`, `steer-backlog`, `steer+backlog`, `queue`) with debounce, cap, and drop policy |
| **Job system** | Basic scheduler | Full scheduler with cron, interval, and one-shot; `continuous` cron mode; delivery tokens (`NO_REPLY`, `SUMMARIZE`); subagent system built on jobs |
| **Memory** | File-based | Pluggable via hook intercept; default is pyclawops's built-in Vault (structured fact store with lifecycle management and graph-linked retrieval); legacy FileMemoryBackend also available |
| **Skills** | agentskills.io format | Same format; adds `agent`, `channels`, `inject`, `schedule` frontmatter fields |
| **Hooks** | Basic | Two-pattern system: notification (fire-all) + intercept (first-wins); bundled hooks for session-memory and boot-md |
| **Channels** | Telegram | Telegram (multi-bot, topics, thinking spoilers), Slack (threading, pulse), Discord, iMessage, Signal, WhatsApp, Google Chat, LINE, plugin API |
| **Security** | None | Exec approval system (allowlist/denylist/all/none), Docker sandbox, audit logging |
| **A2A** | Not supported | Google A2A protocol; per-agent endpoints mounted on the REST API; shared or isolated session modes |
| **TUI** | None | Full Textual dashboard (agents, sessions, history, jobs, config, skills, traces, log stream) |
| **Reflection** | None | Live architecture reflection via `@reflect_system/event/command` decorators; `reflect()` MCP tool |
| **Config secrets** | Env vars only | `${env:}`, `${keychain:}`, `${file:}`, `${provider:}` inline resolution |
| **Import from OpenClaw** | — | `pyclawops import-openclaw` converts OpenClaw JSONL history to FA format |

### What pyclawops reuses from OpenClaw

- **Skill format**: `SKILL.md` with YAML frontmatter — fully compatible.
- **`fastagent.config.yaml` schema**: pyclawops generates equivalent settings programmatically.
- **Cron expression syntax**: same 5-field cron with timezone support.
- **General philosophy**: one always-active session per agent, channels as routing, skills as injected context.

---

## Configuration Reference

pyclawops reads `~/.pyclawops/config.yaml` (or the path passed to `--config`). The file is YAML. All keys at every level use **camelCase** (the Python models map these to snake_case internally via Pydantic `validation_alias`).

### Inline Secrets

Secret values can be stored outside the config file:

```yaml
apiKey: "${env:MY_API_KEY}"          # from environment variable
apiKey: "${keychain:My Key Name}"    # from macOS Keychain (service = "pyclawops")
apiKey: "${file:~/.my_api_key}"      # from file contents (trimmed)
apiKey: "${provider:my-provider}"    # from a named secrets provider
```

---

### providers

Declares LLM provider credentials and per-model concurrency limits. Each provider maps to a FastAgent provider type. Use `fastagent_provider: generic` for any OpenAI-compatible endpoint.

```yaml
providers:
  anthropic:
    enabled: true
    apiKey: "${env:ANTHROPIC_API_KEY}"
    models:
      claude-sonnet-4-6:
        enabled: true
        concurrency: 3       # max concurrent in-flight calls for this model

  openai:
    enabled: true
    apiKey: "${env:OPENAI_API_KEY}"
    models:
      gpt-4o:
        enabled: true
        concurrency: 5

  # Any OpenAI-compatible endpoint (MiniMax, local Ollama, etc.)
  minimax:
    enabled: true
    fastagent_provider: generic      # use generic OpenAI-compat layer
    api_key: "${env:MINIMAX_API_KEY}"
    api_url: "https://api.minimax.io/v1"
    models:
      MiniMax-M2.7:
        enabled: true
        concurrency: 10
    usage:                           # optional: query a usage/quota endpoint
      enabled: true
      endpoint: "https://platform.minimax.io/v1/api/openplatform/..."
      check_interval: 300            # seconds between checks
      throttle:
        background: 80               # pause background jobs at 80% usage
        normal: 90                   # pause normal messages at 90% usage
```

---

### agents

Each key under `agents` is an agent ID. Agents are independent LLM instances that each maintain their own session history, system prompt, and MCP connections.

```yaml
agents:
  main:
    name: Main                        # display name
    model: anthropic/claude-sonnet-4-6  # provider/model-id
    contextWindow: 200000             # context window in tokens
    use_fastagent: true               # required — use FastAgent execution engine
    show_thinking: false              # show <thinking> blocks to users
    max_iterations: 20                # max agentic loop iterations
    max_tokens: 16384                 # max tokens per response

    request_params:                   # extra provider-specific params
      reasoning_split: true           # MiniMax: split reasoning from response

    mcp_servers:                      # MCP servers to connect to
      - pyclawops                        # pyclawops's built-in tool server (port 8081)
      - fetch                         # HTTP fetch tool
      - time                          # date/time tool
      - filesystem                    # local filesystem access

    tools:
      profile: full                   # full | minimal | none
      # 'full' enables all pyclawops MCP tools
      # 'minimal' enables only core tools
      # 'none' disables the tool profile system

    skills_dirs:                      # extra skill search directories
      - "~/.agents/skills"

    queue:
      mode: followup                  # followup | collect | interrupt | steer |
                                      # steer-backlog | steer+backlog | queue
      debounce_ms: 300                # wait this long for more messages
      cap: 20                         # max queued messages before drop policy
      drop: old                       # old | new | summarize

    vault:                            # pyclawops Vault config for this agent
      enabled: true                   # set to null/false to disable vault
      show_recall: false              # append injected facts to agent replies (debug)
      default_profile: auto           # auto | default | planning | incident | handoff | research

      agent:
        enabled: true
        model: ""                     # empty = use main agent model
        max_tokens: 2048
        channels: [telegram, slack, tui, http]
        min_turns: 2                  # min messages before extraction runs

      lifecycle:
        crystallize_reinforcements: 3
        crystallize_days: 7
        forget_days: 30

      search:
        backend: fallback             # fallback | hybrid (requires qmd)
        injection_limit: 5            # max facts injected per turn
        confidence_threshold: 0.5
        min_relevance_score: 0.5
        min_query_words: 3
        graph_hops: 2                 # BFS depth for wikilink expansion

    a2a:                              # Agent-to-Agent protocol
      enabled: false
      allowInbound: true
      allowOutbound: false
      sessionMode: shared             # shared | isolated
```

**Model string format:** `<provider>/<model-id>`. Examples:
- `anthropic/claude-sonnet-4-6`
- `openai/gpt-4o`
- `minimax/MiniMax-M2.7`
- `generic/my-local-model`

**Built-in MCP servers** (`mcp_servers`): `pyclawops`, `fetch`, `time`, `filesystem`, `chrome-devtools`. Custom MCP servers are defined in `fastagent.config.yaml`.

---

### channels

#### Telegram

```yaml
channels:
  telegram:
    enabled: true
    streaming: true                   # stream responses chunk-by-chunk
    allowedUsers:                     # Telegram user IDs (integers). Empty = allow all.
      - 123456789
    deniedUsers: []
    bots:
      main:                           # bot name (arbitrary)
        botToken: "${env:TELEGRAM_BOT_TOKEN}"
        agent: main                   # which agent handles this bot
      assistant:
        botToken: "${env:TELEGRAM_BOT2_TOKEN}"
        agent: assistant
```

Multiple bots can point to the same or different agents. Telegram user IDs are integers.

**Topic support** (forum groups with threads):
```yaml
agents:
  main:
    topics:
      "12345": ritchie    # thread_id → agent_id
```

#### Slack

```yaml
channels:
  slack:
    enabled: true
    botToken: "${env:SLACK_BOT_TOKEN}"
    appToken: "${env:SLACK_APP_TOKEN}"
    agent: main
    threading: true                   # each thread = its own session
    allowedUsers: []                  # Slack user IDs are strings ("U123ABC")
    deniedUsers: []
    pulse_channel: "C12345678"        # optional: send heartbeat pings here
    pulse_interval_minutes: 60
```

---

### gateway

```yaml
gateway:
  host: 0.0.0.0
  port: 8080                          # REST API + A2A endpoints
  mcp_port: 8081                      # FastMCP tool server (FastAgent connects here)
  log_level: info                     # debug | info | warning | error
  log_retention_days: 7
  debug: false

  skills_dirs:                        # global extra skill search paths
    - "~/.pyclawops/skills"

  hooks_dirs: []                      # extra hook search paths

  a2a:
    enabled: false                    # enable Google A2A protocol endpoints

  browser:
    chromeDevtoolsMcp:
      enabled: false                  # enable chrome-devtools MCP server
```

---

### memory

```yaml
memory:
  backend: file                       # file (default) | clawvault (legacy CLI wrapper)

  # Legacy file backend — daily markdown journals with optional vector search
  embedding:
    enabled: false                    # enable vector search
    provider: openai                  # openai | gemini | local
    model: text-embedding-3-small
    api_key: "${env:OPENAI_API_KEY}"
```

The `memory:` top-level section controls the **legacy** `FileMemoryBackend`. pyclawops's built-in **Vault** is configured per-agent under `agents[].vault:` (see the [agents](#agents) section and [Memory & Vault](#memory--vault) below).

---

### security

```yaml
security:
  exec_approvals:
    mode: allowlist                   # allowlist | denylist | all | none
    safe_bins:                        # commands allowed in allowlist mode
      - ls
      - cat
      - python3
      - uv
    always_approve:                   # patterns that bypass mode check (regex or literal)
      - "uv run *.py"
    denied_users: []                  # global Telegram user ID denylist (integers)

  sandbox:
    enabled: false                    # run bash tool in Docker container
    type: docker                      # only "docker" supported
    image: python:3.12-slim
    memory_mb: 256
    cpu_quota: 0.5

  audit:
    enabled: true
    log_file: "~/.pyclawops/logs/audit.log"
    retention_days: 90
```

---

### sessions

```yaml
sessions:
  ttlHours: 24                        # idle TTL before session reaped from index
  reaperIntervalMinutes: 60           # how often the reaper runs
  maxSessions: 1000                   # max sessions in in-memory index
  sessionTimeout: 3600                # seconds before session marked inactive
  dailyRollover: true                 # archive session and start fresh at midnight
```

---

### jobs

```yaml
jobs:
  enabled: true
  agentsDir: "~/.pyclawops/agents"
  defaultTimezone: "America/New_York"  # used when a cron job has no explicit tz
```

See [Jobs & Subagents](#jobs--subagents) for full job configuration syntax.

---

## Systems Reference

### Gateway System

**File:** `pyclawops/core/gateway.py`

The Gateway is the central orchestrator. It owns all subsystem instances, manages the two HTTP servers (MCP on port 8081 and REST API on port 8080), and routes every inbound message through the full pipeline.

**Startup order** (order matters — FastAgent eagerly connects to MCP servers):

```
1. Load config
2. Start pyclawops MCP server    (FastMCP, port 8081)  ← FastAgent connects here
3. Start REST API server       (FastAPI/uvicorn, port 8080)
4. gateway.initialize()
   → AgentManager   (one Agent + AgentRunner per configured agent)
   → SessionManager (session index + reaper background tasks)
   → JobScheduler   (loads jobs.yaml, starts polling loop)
   → HookRegistry + HookLoader (loads bundled + custom hooks)
   → QueueManager   (per-session message queues)
   → AuditLogger, ExecApprovalSystem
   → mount A2A routes (if enabled)
   → fire gateway:startup hook
5. Start Telegram polling tasks (one asyncio.Task per bot)
6. Run until Ctrl+C
7. gateway.stop() — reverse order: agents → MCP server → API server
```

**Message dispatch pipeline:**

```
inbound message
    → deduplication (TTL map on message_id)
    → allowlist / denylist check (per-channel)
    → SessionManager.get_active_session()
    → CommandRegistry.dispatch()  (handles /slash commands)
    → SessionMessageQueue.enqueue()
    → Agent.handle_message()
    → AgentRunner.run_stream()
    → reply delivered back to originating channel
```

**REST API** (port 8080) exposes:
- `/api/v1/sessions/` — session CRUD
- `/api/v1/jobs/` — job CRUD + triggers
- `/api/v1/memory/` — memory CRUD
- `/api/v1/config` — config read/reload
- `/api/v1/health/detail` — health status
- `/api/v1/usage` — message/token counters
- `/api/v1/tools` — available MCP tools
- `/api/v1/hooks` — registered hooks
- `/docs` — Swagger UI

---

### Agents & AgentRunner

**Files:** `pyclawops/core/agent.py`, `pyclawops/agents/runner.py`

**`Agent`** is the runtime wrapper around an agent's config. Each `Agent` maintains a cache of per-session `AgentRunner` instances keyed by session ID. This preserves per-session conversation history while keeping sessions completely isolated from each other.

**`AgentRunner`** wraps FastAgent and is the boundary between pyclawops's session model and FastAgent's execution engine:

- Holds one `FastAgent` context (`fast.run()`) alive for the runner's lifetime
- Loads history lazily on first use (`_load_history()`) — injects `PromptMessageExtended` JSON directly into FA's message history
- Saves history after every successful turn (`_save_history()`) with atomic file rotation
- Streams responses as `(text_chunk, is_reasoning)` tuples via `run_stream()`
- When `show_thinking=False` (default), strips `<thinking>` blocks before yielding

**Session runner cache:**

```
Agent._session_runners = {
    "2026-03-11-aB3xYz": AgentRunner(history_path=~/.pyclawops/agents/main/sessions/.../history.json),
    "2026-03-11-cD5yWz": AgentRunner(history_path=...),
    ...
}
```

On error, `agent.evict_session_runner(session_id)` closes the runner (disconnects FA MCP connections) and removes it from the cache. The next message creates a fresh runner that reloads history from disk — clean reconnect with preserved context.

**System prompt assembly** (`pyclawops/core/prompt_builder.py`):

Agent bootstrap files live in `~/.pyclawops/agents/{agent_id}/`:

| Flag | File(s) loaded |
|------|----------------|
| `include_personality` | `PERSONALITY.md`, `SOUL.md` |
| `include_identity` | `IDENTITY.md` |
| `include_rules` | `RULES.md` |
| `include_memory` | `MEMORY.md` |
| `include_user` | `USER.md` |
| `include_agents` | `AGENTS.md` |
| `include_tools` | `TOOLS.md` |
| `include_skills` | `<available_skills>` XML block (name + description only) |

All of these are optional. Missing files are silently skipped.

---

### Sessions System

**File:** `pyclawops/core/session.py`

pyclawops uses a **one-active-session-per-agent** model. Each agent has exactly one live session at any time, regardless of how many channels are sending it messages. Channels are routing metadata — the session is not tied to any particular channel.

**Session metadata** (`session.json`):

```json
{
  "id": "2026-03-11-aB3xYz",
  "agent_id": "main",
  "channel": "telegram",
  "last_channel": "slack",
  "last_user_id": "U123456",
  "last_thread_ts": "1710000000.123456",
  "message_count": 8,
  "context": {
    "model_override": "opus"
  }
}
```

`last_channel` / `last_user_id` / `last_thread_ts` are updated on every inbound message and used for reply routing. This means a job result with `report_to_agent` is always delivered to whatever channel last sent a message to that agent.

**Session lifecycle:**

- `/reset` — archives `history.json` to `archived/`, creates a new session, updates the active pointer
- **Daily rollover** (`dailyRollover: true`) — at midnight, archives the current session and creates a fresh one; `last_channel` / `last_user_id` are preserved on the new session
- **Reaper** — background task evicts idle sessions from the in-memory index; files on disk are never deleted

**History files are never deleted.** The reaper only removes entries from the in-memory index.

---

### Message Queue

**File:** `pyclawops/core/queue.py`

Each session gets its own `SessionMessageQueue` that controls what happens when new messages arrive while the agent is still processing the previous one.

| Mode | Behaviour |
|------|-----------|
| `followup` | Process messages in order. Pending messages wait. **(default)** |
| `collect` | Batch all pending messages into a single dispatch. |
| `interrupt` | Cancel the current turn; only process the newest message. |
| `steer` | Cancel the current turn; combine original + correction into a steering prompt. |
| `steer-backlog` | Never cancel; after the current turn, combine backlog with steer framing. |
| `queue` | Strict FIFO; no cancellation, no combining, no debounce. |

**Debounce** (default 300ms): after a message arrives, the queue waits briefly for more messages before dispatching — prevents rapid-fire messages from triggering multiple agent turns.

Configure per-agent:

```yaml
agents:
  main:
    queue:
      mode: steer
      debounce_ms: 500
      cap: 20        # max queued before drop policy activates
      drop: old      # old | new | summarize
```

---

### Commands

**File:** `pyclawops/core/commands.py`

The `CommandRegistry` dispatches `/slash` commands before messages reach the agent. Built-in commands:

| Command | Description |
|---------|-------------|
| `/help` | List available commands |
| `/reset` | Archive history, start a new session |
| `/new` | Alias for /reset |
| `/status` | Show gateway status (uptime, sessions, jobs) |
| `/model <name>` | Override the model for the current session |
| `/models` | List available models |
| `/think [on\|off\|level]` | Toggle reasoning display |
| `/usage` | Show token/message usage |
| `/context` | Show current session context |
| `/job <subcommand>` | Manage scheduled jobs |
| `/skills` | List available skills |
| `/skill <name>` | Invoke a skill |
| `/subagents` | Manage running subagents |
| `/queue <subcommand>` | Inspect the message queue |
| `/history` | Show session history summary |
| `/clear` | Clear TUI chat display |
| `/mcp` | List MCP tools |
| `/cards` | List A2A agent cards |
| `/agent <subcommand>` | Switch agents or list agents |
| `/bash <command>` | Execute a shell command directly |
| `/allowlist <subcommand>` | Manage channel allowlists |
| `/reasoning <level>` | Set reasoning effort |
| `/memories` | Search or list memory entries |
| `/forget <key>` | Delete a memory entry |
| `/ingest <url\|text>` | Ingest content into vault memory |
| `/debug` | Toggle debug mode |
| `/reload` | Reload config without restarting |
| `/restart` | Restart the gateway |
| `/config <get\|set\|delete>` | Live config editing |
| `/exec <subcommand>` | Manage exec approvals |
| `/elevated` | Toggle elevated mode |
| `/activation` | View agent activation state |
| `/verbose` | Toggle verbose logging |
| `/approve <command>` | Manually approve an exec request |
| `/export` | Export session history |
| `/focus / /unfocus` | Focus/unfocus agent attention |
| `/send <agent> <msg>` | Send a message to another agent session |
| `/acp` | Agent communication protocol actions |
| `/tts <text>` | Text-to-speech via MiniMax TTS |

---

### Jobs & Subagents

**File:** `pyclawops/jobs/scheduler.py`

Jobs are persistent, scheduled tasks. The scheduler supports three schedule types:

**Schedule types:**

```yaml
# Cron (standard 5-field + timezone)
schedule:
  kind: cron
  expr: "0 9 * * 1-5"
  timezone: "America/New_York"
  stagger_seconds: 0    # random jitter

# Cron — continuous mode (replace minutes with "continuous")
schedule:
  kind: cron
  expr: "continuous 7-14 * * 1-5"  # restart immediately after each run during window
  timezone: "America/New_York"

# Interval
schedule:
  kind: interval
  seconds: 3600         # interval starts after previous run ends

# One-shot
schedule:
  kind: at
  at: "2026-06-01T09:00:00"
```

**Run types:**

```yaml
# Shell command
run:
  kind: command
  command: "cd /some/dir && uv run python script.py"

# Agent prompt
run:
  kind: agent
  agent: ritchie
  message: "Run the trading-scan skill."
  session_mode: isolated      # isolated | persistent
  prompt_preset: full         # full | minimal | task
  include_memory: true        # fine-grained prompt control
  report_to_agent: niggy      # deliver result to this agent's active channel
```

**Delivery:**

```yaml
# Announce to a channel
deliver:
  mode: announce
  channel: telegram
  chat_id: "12345678"

# Webhook
deliver:
  mode: webhook
  url: "https://example.com/webhook"

# Silent (side-effect jobs)
deliver:
  mode: none
```

**Delivery tokens** (for `report_to_agent`): The isolated agent's response can start with a token on the first line:

| Token | Effect |
|-------|--------|
| `NO_REPLY <note>` (≤100 chars total) | Suppress delivery; still inject into target history |
| `SUMMARIZE <content>` | Pass to target agent's LLM for summarization before delivery |
| *(no token)* | Verbatim delivery to target agent's active channel |

**Subagents** are ephemeral jobs spawned from within a conversation via the `subagent_spawn` MCP tool. They run immediately, are never written to `jobs.yaml`, and return results to the calling session via `report_to_session`. MCP tools: `subagent_spawn`, `subagents_list`, `subagent_status`, `subagent_kill`, `subagent_interrupt`, `subagent_send`.

**Slash commands:**

```
/job list                          — list all jobs
/job add <agent> "<message>" every <N>s|m|h
/job add <agent> "<message>" cron "<expr>"
/job enable <name>
/job disable <name>
/job run <name>                    — trigger immediately
/job delete <name>
```

---

### Hooks

**Files:** `pyclawops/hooks/registry.py`, `pyclawops/hooks/events.py`, `pyclawops/hooks/loader.py`

The hook system is pyclawops's event-driven extension mechanism. Two handler patterns:

**Notification** — all registered handlers fire; return values ignored; exceptions caught and logged. Used for logging, auditing, side-effects.

**Intercept** — handlers fire in priority order; first non-`None` return value wins; remaining handlers skipped. Used for `memory:*` operations so plugins can transparently replace the backend.

**Hook events:**

| Event | Pattern | Fired when |
|-------|---------|------------|
| `gateway:startup` | notify | Gateway fully initialized |
| `gateway:shutdown` | notify | Gateway stopping |
| `message:received` | notify | Inbound message before agent |
| `message:sent` | notify | Outbound reply sent |
| `command:reset` | notify | `/reset` executed |
| `command:*` | notify | Any slash command |
| `session:created` | notify | New session created |
| `session:expired` | notify | Reaper evicts idle session |
| `agent:after_response` | notify | Agent finishes a turn |
| `tool:before_exec` | notify | Before MCP tool call |
| `tool:after_exec` | notify | After MCP tool call |
| `memory:read` | **intercept** | Memory read operation |
| `memory:write` | **intercept** | Memory write operation |
| `memory:delete` | **intercept** | Memory delete operation |
| `memory:search` | **intercept** | Memory search operation |
| `memory:list` | **intercept** | Memory list operation |

**Bundled hooks** (always available):

- **`session-memory`** (`command:reset`) — writes conversation history to the agent's memory file before archiving
- **`boot-md`** (`gateway:startup`) — injects the agent's `MEMORY.md` into context on startup

**Custom hooks:**

```yaml
hooks:
  custom:
    - event: message:received
      handler: /path/to/my_hook.py
      priority: 10
      description: "My custom hook"
```

---

### Memory & Vault

**Files:** `pyclawops/memory/service.py`, `pyclawops/memory/vault/`

All memory operations go through `MemoryService`, which routes them through the `HookRegistry` intercept chain. Any plugin can swap the backend by registering a `memory:*` intercept handler.

pyclawops ships a built-in **Vault** — a structured, per-agent fact store. It lives at `~/.pyclawops/agents/{agent_id}/vault/` and is configured per-agent under `agents[].vault:`.

#### Memory Types

The Vault categorises facts into 14 built-in types, each with its own injection weight and keywords:

| Type | Description |
|------|-------------|
| `preference` | User preferences and settings |
| `fact` | General factual knowledge |
| `instruction` | Persistent behavioural instructions |
| `rule` | Hard constraints the agent must follow |
| `goal` | Long-term objectives |
| `context` | Background contextual information |
| `event` | One-time occurrences (with timestamp) |
| `relationship` | Information about people or systems |
| `task` | Actionable to-dos |
| `decision` | Recorded decisions and their rationale |
| `hypothesis` | Uncertain beliefs pending confirmation |
| `summary` | Condensed information from longer content |
| `reference` | Pointers to external resources |
| `skill` | Learned capabilities or procedures |

Custom types can be added under `vault.types` in the agent config.

#### Fact Structure

Each fact is a Markdown file named with a ULID (sortable by creation time):

```
~/.pyclawops/agents/{agent_id}/vault/
├── .cursors.json          ← ingestion progress + crash recovery
├── facts/
│   ├── 01KMGEGB....md    ← active facts
│   └── ...
└── archive/
    └── 01KMGFA0....md    ← superseded / archived facts
```

Every fact file has YAML frontmatter followed by the fact text:

```markdown
---
id: 01KMGEGB...
type: preference
confidence: 0.9
reinforcements: 2
state: crystallized    # provisional | crystallized | superseded | archived
tier: 1                # 1–4 (tier 4 is least relevant / approaching expiry)
created_at: 2026-03-01T12:00:00Z
last_seen_at: 2026-03-20T09:30:00Z
source_session: 2026-03-01-ab1234
tags: [ui, display]
links: []              # wikilinks to related fact IDs
---
User prefers concise responses with no trailing summaries.
```

**Lifecycle states:**

- **`provisional`** — freshly extracted; confidence < threshold or fewer than `crystallize_reinforcements` reinforcements
- **`crystallized`** — confirmed by repeated reinforcement or age past `crystallize_days`; higher injection weight
- **`superseded`** — replaced by a newer fact (moved to `archive/`)
- **`archived`** — forgotten or manually removed (moved to `archive/`)

**Tier compression:** facts age through tiers 1→4. Tier 1 facts are injected first; tier 4 facts are eligible for forgetting after `forget_days`.

#### Ingestion Pipeline

After each conversation turn (and on a catch-up pass at startup), the Vault processes new messages through:

1. **Cursor check** — skip segments already processed (tracked in `.cursors.json`)
2. **Related-facts search** — retrieve relevant existing facts for LLM context (avoids duplicates)
3. **LLM extraction** — a dedicated sub-agent extracts discrete facts from the conversation segment
4. **3-layer deduplication:**
   - **LLM context layer** — existing related facts are provided to the extraction prompt so the LLM avoids re-stating them
   - **Jaccard gate** — extracted facts with ≥ 0.70 token overlap with an existing fact are dropped
   - **Reweave pass** — semantically similar surviving facts trigger a merge/supersede step
5. **Auto-linking** — new facts gain `[[wikilink]]` references to related existing facts
6. **Cursor advance** — progress committed; `currently_processing` cleared

Crash recovery: if `currently_processing` is non-null at startup, the previous run crashed mid-ingestion. The marker is cleared and that segment is re-processed.

#### Retrieval & Context Injection

At each turn, the Vault scores and injects relevant facts:

- **Search backends:** `fallback` (keyword scoring with type-weight multiplier) or `hybrid` (RRF fusion of keyword + vector via qmd)
- **Injection guards:** short queries (`< min_query_words` words) skip injection entirely; `injection_limit` caps facts per turn
- **Query intent classification:** the query is classified into intents (task, planning, recall, etc.) and a per-intent multiplier boosts the relevance threshold
- **Keyword boosting:** type-specific keywords that appear in the query boost that type's score by 1.5×
- **Graph expansion:** BFS from matched facts across `[[wikilink]]` edges up to `graph_hops` depth; linked facts are added at a discounted score
- **Seen-fact cache:** facts already injected in the current session are suppressed on subsequent turns (cleared on `/reset`)

**Retrieval profiles** control which types are prioritised. The profile is auto-inferred from the query unless `default_profile` is set:

| Profile | Prioritises |
|---------|-------------|
| `default` | Balanced mix |
| `planning` | goals, tasks, decisions |
| `incident` | events, context, hypotheses |
| `handoff` | summaries, relationships, references |
| `research` | facts, hypotheses, references |

#### MCP Tools

`memory_store`, `memory_get`, `memory_search`, `memory_list`, `memory_delete`, `memory_reindex`, `vault_recall`, `vault_fact_store`.

`vault_recall(query)` runs the full scored retrieval pipeline and returns formatted facts. `vault_fact_store(text, type, confidence)` manually records a fact.

#### Legacy FileMemoryBackend

The legacy backend stores entries as daily markdown journals:
- `~/.pyclawops/agents/{id}/memory/YYYY-MM-DD.md` — daily journal (appended by tools)
- `~/.pyclawops/agents/{id}/memory/MEMORY.md` — curated file (edited by user; never written by tools)

`MEMORY.md` is injected into the agent's system prompt at startup via the `boot-md` hook and via `include_memory` in job prompts. Edit it directly to give your agent permanent context.

---

### Skills

**Files:** `pyclawops/skills/registry.py`

Skills are modular, user-installable capability packages. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter and a markdown body, plus optional scripts and reference files.

**Directory structure:**

```
~/.pyclawops/skills/
  my-skill/
    SKILL.md           ← required
    scripts/
      do_thing.py      ← PEP 723 inline deps, run via uv
      helper.sh
    references/
      api-schema.md
```

**`SKILL.md` format:**

```yaml
---
name: my-skill
description: |
  What this skill does and when to use it.
  Triggers on: "summarize", "make a summary"
version: "1.0"
allowed-tools: [memory, jobs]
agent: main             # which agent (default: default agent)
channels: [telegram]    # available channels (default: all)
---

# My Skill

Instructions for the agent. Keep this short.
Tell the agent which script to run and with what arguments.

Run: `uv run {skill_dir}/scripts/do_thing.py --arg "$INPUT"`
```

`{skill_dir}` is substituted with the absolute path to the skill directory at injection time — scripts are always findable regardless of working directory.

**Search order** (later overrides earlier on name collision):
1. `~/.pyclawops/skills/` (global)
2. `~/.pyclawops/agents/{agent_id}/skills/` (per-agent)
3. Extra dirs from `gateway.skills_dirs` config

**Invocation:**
- `/skills` — list discovered skills
- `/skill <name>` — inject skill body + forward message to agent
- MCP tool: `skills_list()`, `skill_read(name)`
- Skills are auto-exposed as `skill://` MCP resources via FastMCP

---

### Channels System

**Files:** `pyclawops/channels/plugin.py`, `pyclawops/channels/`

Channel adapters are the boundary between external messaging platforms and the Gateway. Each adapter implements the `ChannelPlugin` ABC.

**Built-in channels:** Telegram, Slack, Discord, Google Chat, iMessage, LINE, Signal, WhatsApp.

**Telegram specifics:**
- Supports multiple bots per gateway (each bot → its own polling task)
- Message splitting at 4096-char limit (split on paragraph → newline → hard boundary)
- Thinking content rendered as `<blockquote expandable>` spoiler
- Typing indicator refreshed every 4 seconds
- Forum topic routing via `message_thread_id`

**Slack specifics:**
- `threading: true` — each thread becomes its own pyclawops session
- Optional pulse heartbeat to a monitoring channel

**Third-party plugins** are discovered via `pyclawops.channels` entry point group or `plugins.channels` list in config.

---

### Security System

**Files:** `pyclawops/security/approvals.py`, `pyclawops/security/sandbox.py`, `pyclawops/security/audit.py`

**Exec Approvals** control whether `bash` tool calls are permitted:

| Mode | Behaviour |
|------|-----------|
| `allowlist` | Only `safe_bins` commands allowed |
| `denylist` | `deny_list` blocked; all others allowed |
| `all` | All commands permitted |
| `none` | All commands denied |

`always_approve` patterns bypass mode checks entirely (useful for `uv run` patterns).

**Sandbox** (optional): runs bash tool commands inside Docker containers with restricted networking, memory, CPU, and filesystem access.

**Audit Logger** appends JSON-lines records to `~/.pyclawops/logs/audit.log` for every inbound message, tool execution, and outbound reply. Exposed via `audit_log_tail()` and `audit_log_search()` MCP tools.

---

### MCP Server

**File:** `pyclawops/tools/server.py`

The pyclawops MCP server runs on port 8081 (default) using FastMCP. FastAgent connects to it during agent initialization to discover and call tools. The gateway injects `X-Agent-Name` in request headers so tools can identify the calling agent.

**Tool categories:**

| Category | Tools |
|----------|-------|
| Execution | `bash` — shell execution with exec-approval policy |
| Web | `web_search` — DuckDuckGo; `image` — vision model; `tts` — text-to-speech |
| Sessions | `sessions_list`, `sessions_history`, `sessions_send`, `sessions_spawn` |
| Memory | `memory_store`, `memory_get`, `memory_search`, `memory_list`, `memory_delete`, `memory_reindex` |
| Vault | `vault_recall`, `vault_fact_store` |
| Jobs | `job_list`, `job_get`, `job_create`, `job_update`, `job_delete`, `job_enable`, `job_disable`, `job_run_now`, `job_history` |
| Subagents | `subagent_spawn`, `subagents_list`, `subagent_status`, `subagent_kill`, `subagent_interrupt`, `subagent_send` |
| Todos | `todo_list`, `todo_add`, `todo_update`, `todo_delete` |
| Config | `config_get`, `config_set`, `config_delete`, `config_validate`, `config_reload`, `config_schema` |
| Skills | `skills_list`, `skill_read` |
| A2A | `a2a_list_agents`, `a2a_send_message`, `a2a_get_card` |
| Audit | `audit_log_tail`, `audit_log_search` |
| Workflows | `workflow_chain`, `workflow_parallel` |
| Agents | `agents_list`, `session_status`, `send_message` |
| Reflection | `reflect`, `reflect_source` |

**`reflect()` and `reflect_source()`** let agents explore pyclawops's own architecture:

```
reflect()                              → architecture overview
reflect(category="system")             → list all registered systems
reflect(category="system", name="gateway")  → gateway detail
reflect(category="config", name="agents")   → agents config schema
reflect_source("core/gateway.py")      → source with line numbers
```

---

### A2A Protocol

**Files:** `pyclawops/a2a/executor.py`, `pyclawops/a2a/setup.py`

A2A (Agent-to-Agent) is a Google-defined protocol that lets external AI systems call pyclawops agents over HTTP using a standardised JSON-RPC interface. A2A routes are mounted onto the existing REST API (port 8080).

**Endpoints (per agent):**

```
GET  /a2a/{agent_id}/.well-known/agent.json   # agent card (capabilities + skills)
POST /a2a/{agent_id}/                         # tasks/send JSON-RPC
GET  /a2a/{agent_id}/agent/authenticatedExtendedCard
```

**Session modes:**

| Mode | Behaviour |
|------|-----------|
| `shared` (default) | Routes into the agent's single active session — same context as Telegram/TUI |
| `isolated` | Each A2A task gets its own fresh session |

**Enable:**

```yaml
gateway:
  a2a:
    enabled: true

agents:
  main:
    a2a:
      enabled: true
      sessionMode: shared
```

Requires `pip install a2a-sdk`. If not installed, A2A is silently disabled.

---

### Reflection

**Files:** `pyclawops/reflect/__init__.py`, `pyclawops/reflect/registry.py`, `pyclawops/reflect/decorators.py`

The reflection system allows agents (and developers) to explore pyclawops's architecture live, without reading static documentation. Decorators annotate classes at import time and populate a global registry.

**Decorators:**

```python
from pyclawops.reflect import reflect_system, reflect_event, reflect_command

@reflect_system("gateway")
class Gateway:
    """The main orchestrator..."""

@reflect_event("hook-events")
class HookEvent:
    """Named hook event constants..."""

# For inner functions / closures:
reflect_command("/reset")(cmd_reset_fn)
```

Multiple objects decorated with the same `(category, name)` pair have their docstrings merged and sorted by source location.

**Query API (via `reflect()` MCP tool):**

```
reflect()                                    → architecture overview
reflect(category="system")                   → list all registered systems
reflect(category="system", name="gateway")   → gateway detail
reflect(category="event")                    → list all hook events
reflect(category="command")                  → list all slash commands
reflect(category="config")                   → list config sections
reflect(category="config", name="agents")    → agents config schema
reflect(name="jobs")                         → cross-category search
reflect_source("core/gateway.py")            → source with line numbers
```

The registry is populated at import time. All subsystem modules are imported during gateway startup, so the full registry is available before any agent makes a tool call.

---

### TUI Dashboard

**Files:** `pyclawops/tui/dashboard.py`, `pyclawops/tui/screens.py`

A [Textual](https://textual.textualize.io/) terminal UI that runs in the same process as the gateway. Enabled by default; use `--headless` to skip it.

```
┌─────────────────────────────────────────────────────┐
│  pyclawops v0.2.0 │  3 sessions │  2 jobs │  uptime    │
├─────────────────────────────────────────────────────┤
│  [0 Agents] [1 Sessions] [2 History] [3 Jobs] ...   │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Detail pane (changes per tab)                      │
│                                                     │
├─────────────────────────────────────────────────────┤
│  Live log stream                                    │
└─────────────────────────────────────────────────────┘
```

**Tabs:**

| Key | Tab | Content |
|-----|-----|---------|
| `0` | Agents | Agent list with status |
| `1` | Sessions | Active sessions table |
| `2` | History | Message history for selected session |
| `3` | Jobs | Jobs with status and next-run time |
| `4` | Sys-Prompt | System prompt for selected agent |
| `5` | Config | Current config (secrets redacted) |
| `6` | Files | File browser for `~/.pyclawops/` |
| `7` | Skills | Discovered skills |
| `8` | Run-Hist | Job run history |
| `9` | Agent-Log | Per-agent log viewer |
| `t` | Traces | OpenTelemetry span viewer |

**Key bindings:** `0`–`9`, `t` (switch tabs), `r` (refresh), `v` (view detail), `[`/`]` (resize panes), `F5` (full refresh), `q`/`Ctrl+C` (quit).

---

## Development

```bash
# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/test_commands.py

# Run a specific test
uv run pytest tests/test_commands.py::test_help_command -v
```

Always use `uv run` — never `.venv/bin/pytest` or bare `python`.

OpenClaw source at `~/github/openclaw` can be useful as a design reference for features whose intent isn't clear from the pyclawops codebase alone, but do not mirror its implementation directly.
