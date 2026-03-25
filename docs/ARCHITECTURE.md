# pyclawops Architecture

pyclawops is a modular AI agent platform that connects LLM agents to messaging channels, tools, memory, and scheduling. All subsystems are wired together by the **Gateway**.

---

## Subsystems

### 1. Gateway (`pyclawops/core/gateway.py`)

The central orchestrator. Owns and initialises every other subsystem, manages the async lifecycle (`start()` / `stop()`), routes inbound messages from channels through the agent and back out, and fires hook events at each stage.

**Key responsibilities:**
- Bootstrap: load config, create all subsystem instances
- Inbound deduplication (`_seen_message_ids` with TTL)
- Channel registration and dispatch
- Streaming Telegram responses via `_stream_telegram_response`
- Slash command routing via `CommandRegistry`
- Job scheduling and agent job execution (`_agent_executor`)

---

### 2. Router (`pyclawops/core/router.py`)

Lightweight value-object layer: `IncomingMessage` and `OutgoingMessage` dataclasses plus `MessageRouter` for routing decisions. The Gateway uses it to decide which agent / session should handle a message.

---

### 3. Session Manager (`pyclawops/core/session.py`)

Persists user sessions to disk (`~/.pyclawops/sessions/`). Each session holds a chat history, optional model override, and arbitrary context dict. A background **reaper task** evicts sessions idle longer than `sessions.ttl_hours`.

**Config:** `sessions.persist_dir`, `sessions.ttl_hours`, `sessions.reaper_interval_minutes`

---

### 4. Agent Manager & Factory (`pyclawops/core/agent.py`, `pyclawops/agents/factory.py`)

`AgentManager` owns the map of named agents (from config `agents:` block) and creates `Agent` wrappers on demand. `AgentFactory` decides whether to build a FastAgent-backed runner or fall back to a direct provider call.

---

### 5. Agent Runner (`pyclawops/agents/runner.py`)

Wraps FastAgent for a single named agent. Handles:
- Model initialisation and MCP server wiring
- `request_params` routing: known FastAgent fields → `RequestParams`; unknown fields → `extra_body` (enables provider-specific extensions like `reasoning_split`)
- Per-model concurrency limiting via `ConcurrencyManager`
- `run()` — single-shot call; `run_stream()` — async iterator of `(text, is_reasoning)` tuples
- Stripping `<thinking>` tags (`strip_thinking_tags`) unless `show_thinking=True`
- Monkey-patch for `delta.reasoning_details` (`_patch_openai_llm_for_reasoning_details`) — applied once at class level when a `generic.*` model is used

---

### 6. Command Registry (`pyclawops/core/commands.py`)

Dispatches slash commands (`/help`, `/reset`, `/status`, `/model`, `/job`, ...). Built-in commands are registered at Gateway startup. Plugins can add custom commands.

---

### 7. Concurrency Manager (`pyclawops/core/concurrency.py`)

Per-model semaphore pool. Config: `concurrency.default` (global cap) and `concurrency.models.<model_name>` overrides. `AgentRunner.run()` acquires the semaphore for its model before calling FastAgent.

---

### 8. Prompt Builder (`pyclawops/core/prompt_builder.py`)

Assembles the system prompt for an agent from static `system_prompt` config plus injected context (memory, date, session info, etc.).

---

### 9. Compaction (`pyclawops/core/compaction.py`)

Token-budget management: when conversation history grows beyond a threshold, older messages are summarised and replaced to keep the context window within limits.

---

## Channel Adapters (`pyclawops/channels/`)

Each channel adapter receives messages from an external platform and calls back into the Gateway.

| Module | Channel | Wired in Gateway |
|---|---|---|
| `telegram.py` | Telegram Bot API (polling, streaming edits, typing indicator) | ✅ Yes |
| `slack.py` | Slack Events API (threading, pulse channel) | ✅ Yes |
| `discord.py` | Discord bot | Adapter exists; not yet wired |
| `googlechat.py` | Google Chat | Adapter exists; not yet wired |
| `imessage.py` | iMessage (macOS) | Adapter exists; not yet wired |
| `line.py` | LINE Messaging API | Adapter exists; not yet wired |
| `signal.py` | Signal | Adapter exists; not yet wired |
| `whatsapp.py` | WhatsApp Cloud API | Adapter exists; not yet wired |

**`base.py`** — `ChannelAdapter` ABC all adapters implement.
**`loader.py`** — discovers and instantiates adapters from config + plugin entry points.
**`plugin.py`** — `ChannelPlugin` ABC for third-party channel packages; discovered via entry points (`pyclawops.channels` group) or explicit `plugins.channels` list in config.

**Per-channel config fields:** `enabled`, `botToken`, `allowedUsers`, `deniedUsers`, `streaming`, `typingIndicator`, `topics` (Telegram forum topics), `threading` (Slack).

---

## Config & Secrets (`pyclawops/config/`)

### Config Loader (`loader.py`)

Reads `~/.pyclawops/config/pyclawops.yaml` (or the path given at startup). Resolves inline secret references (`${env:X}`, `${keychain:X}`, `${file:X}`) before handing the dict to Pydantic.

### Config Schema (`schema.py`)

Pydantic models for the full config tree: `GatewayConfig`, `AgentConfig`, `TelegramConfig`, `SlackConfig`, `SecurityConfig`, `MemoryConfig`, `SessionsConfig`, `JobsConfig`, `TuiConfig`, etc.

- Supports both `camelCase` and `snake_case` keys via `AliasChoices`
- `AgentConfig.request_params: Dict[str, Any]` — forwarded to provider API call

### Secrets Manager (`pyclawops/secrets/manager.py`)

Resolves `${source:id}` placeholders. Backends: environment variables, macOS Keychain (`security` CLI), plain files. Used exclusively by `ConfigLoader` during config load.

---

## Security (`pyclawops/security/`)

### Exec Approvals (`approvals.py`)

Controls whether a `bash` tool call is allowed. Modes: `allowlist` (only `safe_bins`), `denylist`, `all`, `none`. The `always_approve` list bypasses the mode check.

### Sandbox (`sandbox.py`)

Wraps shell execution in Docker when `security.sandbox.enabled=true`. Limits network (`none`), memory, CPU, PIDs, and uses a read-only rootfs with a tmpfs scratch area.

### Audit Logger (`audit.py`)

Appends JSON-lines audit records to `~/.pyclawops/logs/audit.log`. Records every inbound message, tool execution, and outbound reply. Configurable retention via `security.audit.retention_days`.

---

## Jobs & Scheduling (`pyclawops/jobs/`)

### Job Scheduler (`scheduler.py`)

Runs cron, interval, and one-shot jobs. Run types are `CommandRun` (shell command via subprocess) and `AgentRun` (agent prompt via `gateway._agent_executor()`). Jobs survive restarts; stored in `~/.pyclawops/agents/{agent_id}/jobs.yaml` (per agent). Run logs appended to `~/.pyclawops/agents/{agent_id}/runs/{job_id}.jsonl`.

Agent jobs support: isolated vs persistent session modes, granular `include_*` system prompt flags, prompt presets (`full`/`minimal`/`task`), delivery tokens (`NO_REPLY`/`SUMMARIZE`), and `report_to_agent` for cross-agent result delivery. See [docs/JOBS.md](JOBS.md) for the full model.

### Job Models (`models.py`)

`Job` → `run: CommandRun | AgentRun`, `schedule: CronSchedule | IntervalSchedule | AtSchedule`, `deliver: DeliverNone | DeliverAnnounce | DeliverWebhook`, `on_failure: FailureAlert | None`, plus runtime state fields (`status`, `next_run`, `last_run`, `consecutive_errors`, etc.).

---

---

## Memory (`pyclawops/memory/`)

Long-term persistent memory for agents. Supports multiple backends and optional vector search.

| Module | Purpose |
|---|---|
| `service.py` | `MemoryService` — hook-interceptable CRUD + search, falls back to configured backend |
| `backend.py` | `MemoryBackend` ABC |
| `file_backend.py` | **Default backend** — append-only daily markdown journals in `~/.pyclawops/agents/{id}/memory/`; optional `vectors.json` embedding index |
| `embeddings.py` | Embedding providers (OpenAI, Gemini, local/OpenAI-compat HTTP); pure-Python cosine similarity |
| `vault/` | **Optional per-agent structured fact store** — ULID-keyed Markdown files with YAML frontmatter; 13 semantic types; `provisional→crystallized→superseded→archived` lifecycle; FallbackSearch (keyword) or HybridSearch (BM25+vector via RRF). Configured per-agent under `agents[].vault:`. See [docs/VAULT.md](VAULT.md). |

**Config:** `memory.backend` (`file` is the default; `clawvault` is a legacy CLI-wrapper that is no longer recommended). Per-agent Vault is configured under `agents[name].vault:` — it is separate from `memory.backend`.

Hook events `memory:read/write/delete/search/list` are **interceptable** — a plugin can transparently replace the backend.

---

## Hooks (`pyclawops/hooks/`)

Event-driven extension system. Two handler contracts:

- **Notification** (`notify`): all handlers run; return values ignored. Used for side-effects (logging, audit, session-memory saves).
- **Interceptable** (`intercept`): first handler returning non-`None` wins. Used for `memory:*` so plugins can swap backends.

### Hook Registry (`registry.py`)

Maintains `event → [HookRegistration]` map sorted by priority. Handlers can be Python async callables or subprocess-backed file handlers.

### Hook Loader (`loader.py`)

Reads `hooks.bundled` and `hooks.custom` from config, wraps file-based handlers in subprocess shims, and registers everything with the registry.

### Bundled Hooks

| Hook | Event | Action |
|---|---|---|
| `session-memory` | `command:reset` | Save session history to memory before clearing |
| `boot-md` | `gateway:startup` | Inject `BOOT.md` (`~/.pyclawops/BOOT.md` or `~/BOOT.md`) into agent context |

### Hook Events (`events.py`)

| Event | Type | Fired when |
|---|---|---|
| `gateway:startup` | notify | Gateway finishes initialising |
| `gateway:shutdown` | notify | Gateway is stopping |
| `message:received` | notify | Inbound message before agent |
| `message:sent` | notify | Outbound reply after agent |
| `command:reset` | notify | `/reset` slash command |
| `command:*` | notify | Any slash command |
| `session:created` | notify | New session first message |
| `session:expired` | notify | Reaper evicts idle session |
| `agent:after_response` | notify | Agent finishes responding |
| `tool:before_exec` | notify | Before tool call |
| `tool:after_exec` | notify | After tool call |
| `message:transcribed` | notify | Audio message transcription complete |
| `message:preprocessed` | notify | After initial message preprocessing |
| `command:new` | notify | `/new` session command |
| `memory:read` | intercept | Memory read |
| `memory:write` | intercept | Memory write |
| `memory:delete` | intercept | Memory delete |
| `memory:search` | intercept | Memory search |
| `memory:list` | intercept | Memory list |

---

## Todos (`pyclawops/todos/`)

Lightweight task list for agents. `TodoStore` persists todos to `~/.pyclawops/todos.json`. Exposed via the MCP tool server and the `/api/v1/todos` HTTP endpoint.

---

## Tools & MCP Server (`pyclawops/tools/`)

### MCP Server (`server.py`)

A `fastmcp`-based MCP server exposing pyclawops-native tools to FastAgent. Runs as a subprocess; the gateway's own FastAgent instances connect to it via HTTP.

**Tools exposed (61 total):**

*Core execution*

| Tool | Description |
|---|---|
| `bash` | Shell execution with security policy (allowlist/denylist/sandbox) |
| `web_search` | DuckDuckGo search (no API key) |
| `send_message` | Send to configured channels |
| `agents_list` | List configured agents |
| `process` | List/kill background processes |
| `image` | Image understanding via vision model |
| `tts` | Text-to-speech via MiniMax TTS API |

*Sessions*

| Tool | Description |
|---|---|
| `sessions_list` | List active gateway sessions |
| `sessions_history` | Get conversation history for a session |
| `sessions_send` | Send a message into another session |
| `session_status` | Current session info |

*Subagents*

| Tool | Description |
|---|---|
| `subagent_spawn` | Spawn a background subagent; returns immediately with a job_id |
| `subagents_list` | List active subagents spawned by the calling agent |
| `subagent_status` | Get status and details of a subagent |
| `subagent_kill` | Cancel a running subagent |
| `subagent_interrupt` | Interrupt and restart a subagent with a new task |
| `subagent_send` | Queue a follow-up message for a running subagent |

*Memory (FileMemoryBackend)*

| Tool | Description |
|---|---|
| `memory_search` | Search long-term memory (vector or keyword) |
| `memory_get` | Get memory entry by key |
| `memory_store` | Store a key/value entry |
| `memory_delete` | Delete a memory entry by key |
| `memory_list` | List memory keys |
| `memory_reindex` | Rebuild vector search index |

*Vault (per-agent structured fact store, optional)*

| Tool | Description |
|---|---|
| `vault_search` | Search vault facts by query |
| `vault_facts_list` | List vault facts with optional type/state filters |
| `vault_fact_store` | Store a single structured vault fact |
| `vault_bulk_ingest` | Bulk ingest text segments into the vault |

*Jobs*

| Tool | Description |
|---|---|
| `jobs_list` | List scheduled jobs with status and next run time |
| `jobs_get` | Get full details of a job by name or ID |
| `jobs_create_command` | Create a scheduled shell command job |
| `jobs_create_agent` | Create a scheduled agent prompt job |
| `jobs_update` | Update a job's schedule, command, or settings |
| `jobs_delete` | Delete a job |
| `jobs_enable` | Enable a disabled job |
| `jobs_disable` | Disable a job without deleting it |
| `jobs_run_now` | Trigger a job immediately |
| `jobs_history` | Get run history for a job |
| `jobs_status` | Show overall job scheduler status |

*Todos*

| Tool | Description |
|---|---|
| `todos_list` | List todos with optional status/agent filter |
| `todo_get` | Get a todo by ID |
| `todo_create` | Create a new todo |
| `todo_update` | Update todo fields |
| `todo_mark` | Mark a todo with a status and optional notes |
| `todo_delete` | Delete a todo |
| `todos_next` | Get the next highest-priority todo |

*Skills*

| Tool | Description |
|---|---|
| `skills_list` | List available skills (global + per-agent) |
| `skill_read` | Read a skill's full content |

*Config*

| Tool | Description |
|---|---|
| `config_get` | Get current config (secrets redacted) |
| `config_set` | Set a config value at a dot-notation path |
| `config_delete` | Delete a config key |
| `config_validate` | Validate config against schema |
| `config_reload` | Reload config from disk |
| `config_schema` | Get schema for a config section |

*Secrets*

| Tool | Description |
|---|---|
| `secrets_list` | List registered secret names (values not exposed) |
| `secret_get` | Get a secret value by name |

*Audit*

| Tool | Description |
|---|---|
| `audit_log_tail` | Tail recent audit log entries |
| `audit_log_search` | Search audit log by field or keyword |

*Workflows*

| Tool | Description |
|---|---|
| `workflow_chain` | Run a sequential chain of agent steps |
| `workflow_parallel` | Run multiple agents in parallel and merge results |

*Reflection*

| Tool | Description |
|---|---|
| `reflect` | Query pyclawops live architecture (systems/events/commands/config) |
| `reflect_source` | Read pyclawops source module with line numbers |

### Tool Policy (`policy.py`)

Evaluates whether a requested tool/binary is allowed given the current security config. Used by the `bash` tool.

---

## Providers (`pyclawops/providers/`)

Thin provider wrappers used for non-FastAgent paths (direct API calls, fallback).

| Module | Provider |
|---|---|
| `anthropic.py` | Anthropic Claude API |
| `openai.py` | OpenAI-compatible APIs |
| `generic.py` | Generic OpenAI-compatible API (MiniMax, Ollama, and any other compat endpoint) |
| `fastagent.py` | FastAgent orchestration layer |

For FastAgent-backed agents the provider is selected by the model string (e.g. `generic.MiniMax-M2.5`, `sonnet`, `haiku`).

---

## Workflows (`pyclawops/workflows/`)

Higher-level multi-agent patterns built on top of `AgentRunner`.

| Module | Pattern |
|---|---|
| `chain.py` | `ChainWorkflow` — sequential: each agent's output feeds the next |
| `parallel.py` | `ParallelWorkflow` — concurrent: multiple agents run simultaneously, results merged |
| `agents_as_tools.py` | `AgentsAsTools` — one orchestrator agent with sub-agents exposed as callable tools |

---

## HTTP API (`pyclawops/api/`)

REST API served alongside the gateway. Mounted at `/api/v1/`.

| Route module | Endpoints |
|---|---|
| `health.py` | `GET /health`, `GET /health/detail` |
| `sessions.py` | `GET /sessions`, `DELETE /sessions/{id}` |
| `config.py` | `GET /config` (redacted), `POST /config/reload` |
| `agents.py` | `GET /agents` |
| `channels.py` | `GET /channels` |
| `jobs.py` | `GET /jobs`, `POST /jobs`, `DELETE /jobs/{id}` |
| `hooks.py` | `GET /hooks` |
| `todos.py` | `GET /todos`, `POST /todos`, `PUT /todos/{id}`, `DELETE /todos/{id}` |
| `tools.py` | `GET /tools` |
| `usage.py` | `GET /usage` |

`app.py` creates the FastAPI application. `nodes.py` implements the optional peer-to-peer node API.

---

## TUI (`pyclawops/tui/`)

Textual-based terminal UI. Run with `pyclawops run --tui`.

| Module | Purpose |
|---|---|
| `app.py` | `PyclawApp` — Textual `App` subclass, sets up screens |
| `screens.py` | `ChatScreen` — main chat view with live streaming, slash command routing, status bar |
| `widgets.py` | Custom Textual widgets (input, message list, etc.) |
| `components/` | Reusable UI components: `MessageBubble`, `StatusIndicator`, `ActionButton` |

Streaming is rendered in-place via `_stream_replace_lines` — chunks replace the placeholder rather than appending new lines.

---

## Skills Registry (`pyclawops/skills/`)

Named capability bundles that agents can be granted. `SkillsRegistry` maps skill profile names (`minimal`, `full`, etc.) to sets of MCP tool names. Agents declare `tools.profile` in config; the registry resolves which tools are enabled.

---

## Utilities (`pyclawops/utils/`)

| Module | Purpose |
|---|---|
| `browser.py` | Headless browser helpers (screenshot, page text extraction) |
| `peekaboo.py` | Content-peek utilities (image preview, file summary) |

---

## Entry Point (`pyclawops/__main__.py`)

CLI entry point. Commands:
- `pyclawops run` — start the gateway (HTTP + channels)
- `pyclawops run --tui` — start with terminal UI
- `pyclawops tools` — start the MCP tool server standalone
- `pyclawops config` — show/validate config

---

## Configuration Reference

Full config lives at `~/.pyclawops/config/pyclawops.yaml`. See `examples/config.yaml` for an annotated reference. Key top-level sections:

```
version, concurrency, sessions, gateway, security, memory, hooks, providers,
agents, jobs, channels, plugins, tui, nodes
```

Inline secret syntax anywhere a string is expected:
- `${env:MY_VAR}` — environment variable
- `${keychain:My Account}` — macOS Keychain (service = `pyclawops`)
- `${file:~/.secret}` — file contents (trimmed)
