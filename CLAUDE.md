# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_gateway.py

# Run a single test by name
uv run pytest tests/test_commands.py::test_help_command -v

# Run the gateway with dashboard TUI (default)
uv run python -m pyclopse

# Run headless (no TUI — stdout only)
uv run python -m pyclopse --headless

# Run with a specific config
uv run python -m pyclopse --config ~/.pyclopse/config.yaml

# Validate config
uv run python -m pyclopse validate
```

Always use `uv run` — never `.venv/bin/pytest` or bare `python`.

## Background

pyclopse is loosely inspired by **OpenClaw**, a TypeScript-based gateway project. It is **not** a port or 1:1 clone — pyclopse uses its own architecture, naming conventions, and Python idioms. When working on a feature that isn't clear from the pyclopse codebase alone, the OpenClaw source at https://github.com/openclaw/openclaw can be a useful reference for understanding the original intent or design, but do not mirror its implementation directly.

## Architecture

### Request Flow

```
Telegram / Slack / TUI / HTTP API
    → Gateway (pyclopse/core/gateway.py)
        → SessionManager — finds/creates session
        → CommandRegistry — handles /slash commands
        → Agent (pyclopse/core/agent.py)
            → AgentRunner (pyclopse/agents/runner.py) — wraps FastAgent
                → FastAgent connects to pyclopse MCP server (port 8081)
                    → tools call REST API (port 8080) for jobs/todos/config
```

### Startup Sequence (important)

Exact order from `__main__.py`:
1. Parse config + load secrets
2. Setup logging (root logger + file handler)
3. Create `Gateway` instance (no servers started yet)
4. Register `SkillProvider` on the FastMCP server (skill:// MCP resources)
5. **Start MCP server** (port 8081) — `gateway.start_mcp_server()`
6. **Start REST API** (port 8080) — `gateway.start_api_server()`
7. **`gateway.initialize()`** — creates agents, FastAgent connects to MCP. **Must be after steps 5+6.**
8. Setup per-agent logging (`setup_agent_logging()` for each agent)
9. Start Telegram polling tasks (if configured)
10. Start TUI dashboard (`run_dashboard()`) or enter headless sleep loop

The MCP server uses `FastMCP.run_http_async()` — FastMCP owns the uvicorn lifecycle. Do not replace this with manual uvicorn management.

### Key Files

| File | Role |
|------|------|
| `pyclopse/core/gateway.py` | Main orchestrator: Telegram, Slack, jobs, sessions, server lifecycle |
| `pyclopse/core/agent.py` | Agent dataclass + session runner cache; `evict_session_runner()` on error |
| `pyclopse/agents/runner.py` | `AgentRunner` wraps FastAgent; `run_stream()` yields `(text, is_reasoning)` tuples; `strip_thinking_tags()` utility used throughout |
| `pyclopse/tools/server.py` | FastMCP server exposing all built-in tools to agents (port 8081) |
| `pyclopse/api/app.py` | FastAPI REST API (port 8080); used by MCP tools and external clients |
| `pyclopse/core/commands.py` | Slash command dispatcher — 49 commands including `/help`, `/reset`, `/new`, `/status`, `/model`, `/job`, `/skills`, `/skill`, `/subagents`, `/memories`, `/forget`, `/config`, `/reload`, `/restart`, `/think`, `/compact`, `/bash`, and more |
| `pyclopse/core/session.py` | Session persistence + TTL-based reaper |
| `pyclopse/jobs/scheduler.py` | Cron/interval/one-shot job scheduler with `notify_callback`; agent jobs run via `_agent_executor()` in `gateway.py` |
| `pyclopse/config/schema.py` | Pydantic config schema — all fields use `validation_alias` for camelCase YAML |
| `pyclopse/config/loader.py` | Loads `~/.pyclopse/config.yaml`; resolves `${NAME}` references via `SecretsManager` |
| `pyclopse/tui/app.py` | Textual TUI; `pyclopse/tui/screens.py` contains `ChatScreen` with streaming |

### MCP

**FastMCP is the only MCP library used in this project.** We do not use the low-level `mcp` SDK directly, and we do not manage uvicorn ourselves for MCP — FastMCP provides the complete server implementation including its own HTTP transport.

The pyclopse MCP server (`pyclopse/tools/server.py`) is a `FastMCP` instance:

```python
from fastmcp import FastMCP
mcp = FastMCP("pyclopse")

@mcp.tool()
def my_tool(...) -> str: ...
```

It is started via `mcp.run_http_async(host=host, port=port)` — FastMCP internally manages uvicorn, the ASGI app, and the MCP protocol. **Never replace this with direct uvicorn calls or `mcp.http_app()` + manual uvicorn.** If you need to shut it down, cancel the asyncio task (FastMCP handles the rest); suppress the expected log noise in `stop_mcp_server()` rather than taking over the server lifecycle.

FastAgent is an MCP **client** — it connects to MCP servers to use their tools but does not host servers. FastAgent is configured entirely programmatically via `AgentRunner._build_fa_settings()` — there is no `fastagent.config.yaml` file. The gateway is responsible for ensuring MCP servers are running before agents initialize.

- **MCP server (8081)** — `pyclopse/tools/server.py` — FastMCP app; this is what FastAgent connects to for tool calls
- **REST API (8080)** — `pyclopse/api/app.py` — FastAPI/uvicorn app (we do own this one); MCP tools call it internally via `_jobs_api()`, `_todos_api()`, `_config_api()`; also exposed externally at `/docs`

The MCP tools are thin wrappers: agent → MCP tool call → HTTP to REST API → gateway internals.

When a tool receives a 404 from the REST API, use `_fmt_http_err(e, resource_id)` (defined in `server.py`) to return a friendly `[NOT FOUND]` string rather than a raw `[ERROR]`.

### Session Runners

Each agent × session gets its own `AgentRunner` instance cached in `agent._session_runners`. This preserves per-session conversation history. On error, call `agent.evict_session_runner(session_id)` to force a fresh runner on the next message.

On `agent.stop()`, all session runners and the base runner are cleaned up (closes FastAgent MCP connections) before the MCP server is stopped.

### Job Execution

Agent-type jobs run via `_agent_executor()` in `gateway.py`. The job creates an ephemeral session (`session_mode: isolated`) or a shared one (`persistent`), injects a job-specific system prompt built from the `AgentRun` include flags, then calls `handle_message()` to get the response.

**Thinking tag stripping:** Job results always have thinking tags stripped before delivery — regardless of the agent's `show_thinking` setting. This is enforced unconditionally in `_agent_executor()` (via `strip_thinking_tags()` from `runner.py`) before the response is passed to `report_to_agent` or `report_to_session`. The rationale: thinking output from an isolated job agent is internal reasoning noise that should never pollute the receiving agent's context.

### Config Schema

YAML uses camelCase keys; Pydantic models use `validation_alias` or `AliasChoices` to accept them. Always test config parsing with `Model.model_validate({"camelCase": val})` not `Model(snake_case=val)`.

Inline secret syntax: `${NAME}` — looks up `NAME` in the secrets registry loaded from `~/.pyclopse/secrets/secrets.yaml` (falls back to `secrets:` block in pyclopse.yaml). Each registry entry declares `source: env | keychain | file | exec` and its source-specific options. The reference in config YAML is always just `${NAME}` — no source type is embedded in the reference itself. See `pyclopse/secrets/manager.py` and `pyclopse/secrets/models.py`.

### Skills System

Skills live in `~/.pyclopse/skills/` (global) or `~/.pyclopse/agents/{name}/skills/` (per-agent). Each skill is a directory containing a `SKILL.md` with YAML frontmatter (`name`, `description`, `version`, `allowed-tools`) and markdown body. The `{skill_dir}` token is substituted with the absolute skill path at read time. Skills are injected into agent system prompts as `<available_skills>` XML and exposed as `skill://` MCP resources via FastMCP `SkillProvider`.

### Channel Plugin System

Channel plugins implement `ChannelPlugin` ABC from `pyclopse/channels/plugin.py`. Discovery: entry points group `pyclopse.channels` or explicit `plugins.channels` list in config. Each plugin gets a `GatewayHandle` for dispatching inbound messages and sending outbound replies.

### Cross-Channel Sync

`channelSync: true` (default, per-agent) mirrors every message and agent response to all other channels that have interacted with the same agent session. Messages appear natively with no source label.

**Two delivery paths:**
- **Event bus** (`_publish`) — `user_message`, `agent_response`, and `stream_chunk` events consumed by TUI subscribers via `subscribe_agent()` / `_drain_events()` (0.3 s timer).
- **Direct API fan-out** — `_fan_out_user_message` (fire-and-forget `asyncio.create_task`) and `_fan_out_response` send to Telegram/Slack APIs.

**TUI is a first-class channel.** `handle_message(channel="tui")` is called with no `on_chunk`. The gateway always creates a `_bus_chunk` closure for non-job channels that publishes `stream_chunk` events; TUI renders them on the Textual main thread via `_drain_events`. TUI agent switch clears the log and loads the last 40 messages from `session.history_path`.

**Thinking in fan-out.** `agent.handle_message` tracks `_thinking_parts` (is_reasoning=True) and `_response_parts` separately. When `show_thinking=True` and thinking chunks are present, it reconstructs `<thinking>…</thinking>` tags so `format_thinking_for_telegram()` can render expandable blockquotes in fan-out responses — same as the native Telegram streaming path.

**Endpoint tracking.** `_known_endpoints[agent_id][channel]` (in-memory) + `session.context["channel_endpoints"]` (persisted). Each endpoint stores `sender_id`, `sender`, `bot_name`. Restored into `_known_endpoints` on `_get_active_session`. All updates use merge (`setdefault` + field-level) not replace, preserving `bot_name` set by pre-route registration.

### Hook System

Hooks fire on gateway events (`gateway:startup`, `message:received`, `command:reset`, etc.). Bundled hooks: `session-memory` (writes conversation history to memory on reset), `boot-md` (injects `BOOT.md` from `~/.pyclopse/BOOT.md` or `~/BOOT.md` into agent context at startup). Custom hooks are Python scripts registered in config.


### Installation, Updates, and Removal

pyclopse is distributed via PyPI as a `uv tool`.

**Install:**
```bash
uv tool install pyclopse
```

**Update:**
```bash
uv tool upgrade pyclopse
```

**Uninstall:**
```bash
uv tool uninstall pyclopse
```

**Removal (including data):**
```bash
pyclopse uninstall          # removes the binary; prompts whether to delete ~/.pyclopse/
pyclopse uninstall --purge  # removes binary + ~/.pyclopse/ without prompting
```

### Release Workflow

Versioning is managed by `hatch-vcs` — the version is derived automatically from the git tag at install/build time. **Never manually edit the version in `pyproject.toml` or `__init__.py`.** The generated `pyclopse/_version.py` is gitignored.

To cut a release:
```bash
git tag v0.2.0
git push origin v0.2.0
gh release create v0.2.0 --title "v0.2.0" --notes "..." --latest
```

Tag format must be `vMAJOR.MINOR.PATCH`. The `pyclopse update` stable path uses `git ls-remote --tags --sort=-v:refname` to find the latest tag — it only matches tags of this exact format (no pre-release suffixes). Pre-release tags (e.g. `v0.2.0-beta.1`) are ignored by `pyclopse update` stable but reachable via `pyclopse update --version 0.2.0-beta.1`.

### Testing Patterns

```python
# Gateway stub for unit tests — skip __init__
gw = Gateway.__new__(Gateway)
gw._seen_message_ids = {}
gw._dedup_ttl_seconds = 60
gw._usage = {"messages": 0, "tokens": 0}

# Mock concurrency manager in AgentRunner tests
with patch("pyclopse.core.concurrency.get_manager"):
    ...

# Config schema tests use model_validate with camelCase keys
config = ExecApprovalsConfig.model_validate({"mode": "allowlist"})
```

`pytest.ini_options` sets `asyncio_mode = "auto"` — all async tests run without `@pytest.mark.asyncio`.
