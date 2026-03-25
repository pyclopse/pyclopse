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

# Run the gateway (headless)
uv run python -m pyclaw run

# Run with TUI
uv run python -m pyclaw run --tui

# Run with a specific config
uv run python -m pyclaw run --config ~/.pyclaw/config.yaml

# Validate config
uv run python -m pyclaw validate
```

Always use `uv run` — never `.venv/bin/pytest` or bare `python`.

## Background

pyclaw is loosely inspired by **OpenClaw**, a TypeScript-based gateway project. It is **not** a port or 1:1 clone — pyclaw uses its own architecture, naming conventions, and Python idioms. When working on a feature that isn't clear from the pyclaw codebase alone, the OpenClaw source at `~/github/openclaw` can be a useful reference for understanding the original intent or design, but do not mirror its implementation directly.

## Architecture

### Request Flow

```
Telegram / Slack / TUI / HTTP API
    → Gateway (pyclaw/core/gateway.py)
        → SessionManager — finds/creates session
        → CommandRegistry — handles /slash commands
        → Agent (pyclaw/core/agent.py)
            → AgentRunner (pyclaw/agents/runner.py) — wraps FastAgent
                → FastAgent connects to pyclaw MCP server (port 8081)
                    → tools call REST API (port 8080) for jobs/todos/config
```

### Startup Sequence (important)

Both the MCP server (8081) and REST API (8080) **must** start before `gateway.initialize()` because FastAgent eagerly connects to MCP servers during agent initialization. See `__main__.py: run_gateway()`.

The MCP server uses `FastMCP.run_http_async()` — FastMCP owns the uvicorn lifecycle. Do not replace this with manual uvicorn management.

### Key Files

| File | Role |
|------|------|
| `pyclaw/core/gateway.py` | Main orchestrator: Telegram, Slack, jobs, sessions, server lifecycle |
| `pyclaw/core/agent.py` | Agent dataclass + session runner cache; `evict_session_runner()` on error |
| `pyclaw/agents/runner.py` | `AgentRunner` wraps FastAgent; `run_stream()` yields `(text, is_reasoning)` tuples; `strip_thinking_tags()` utility used throughout |
| `pyclaw/tools/server.py` | FastMCP server exposing all built-in tools to agents (port 8081) |
| `pyclaw/api/app.py` | FastAPI REST API (port 8080); used by MCP tools and external clients |
| `pyclaw/core/commands.py` | Slash command dispatcher: `/help /reset /status /model /job /skills /skill` |
| `pyclaw/core/session.py` | Session persistence + TTL-based reaper |
| `pyclaw/jobs/scheduler.py` | Cron/interval/one-shot job scheduler with `notify_callback`; agent jobs run via `_agent_executor()` in `gateway.py` |
| `pyclaw/config/schema.py` | Pydantic config schema — all fields use `validation_alias` for camelCase YAML |
| `pyclaw/config/loader.py` | Loads `~/.pyclaw/config.yaml`; resolves `${source:id}` inline secrets |
| `pyclaw/tui/app.py` | Textual TUI; `pyclaw/tui/screens.py` contains `ChatScreen` with streaming |

### MCP

**FastMCP is the only MCP library used in this project.** We do not use the low-level `mcp` SDK directly, and we do not manage uvicorn ourselves for MCP — FastMCP provides the complete server implementation including its own HTTP transport.

The pyclaw MCP server (`pyclaw/tools/server.py`) is a `FastMCP` instance:

```python
from fastmcp import FastMCP
mcp = FastMCP("pyclaw")

@mcp.tool()
def my_tool(...) -> str: ...
```

It is started via `mcp.run_http_async(host=host, port=port)` — FastMCP internally manages uvicorn, the ASGI app, and the MCP protocol. **Never replace this with direct uvicorn calls or `mcp.http_app()` + manual uvicorn.** If you need to shut it down, cancel the asyncio task (FastMCP handles the rest); suppress the expected log noise in `stop_mcp_server()` rather than taking over the server lifecycle.

FastAgent is an MCP **client** — it connects to MCP servers to use their tools but does not host servers. `fastagent.config.yaml` tells FastAgent where to connect; the gateway is responsible for ensuring those servers are running first.

- **MCP server (8081)** — `pyclaw/tools/server.py` — FastMCP app; this is what FastAgent connects to for tool calls
- **REST API (8080)** — `pyclaw/api/app.py` — FastAPI/uvicorn app (we do own this one); MCP tools call it internally via `_jobs_api()`, `_todos_api()`, `_config_api()`; also exposed externally at `/docs`

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

Inline secret syntax: `${env:VAR}`, `${keychain:Name}`, `${file:path}` — resolved at load time in `pyclaw/config/loader.py`.

### Skills System

Skills live in `~/.pyclaw/skills/` (global) or `~/.pyclaw/agents/{name}/skills/` (per-agent). Each skill is a directory containing a `SKILL.md` with YAML frontmatter (`name`, `description`, `version`, `allowed-tools`) and markdown body. The `{skill_dir}` token is substituted with the absolute skill path at read time. Skills are injected into agent system prompts as `<available_skills>` XML and exposed as `skill://` MCP resources via FastMCP `SkillProvider`.

### Channel Plugin System

Channel plugins implement `ChannelPlugin` ABC from `pyclaw/channels/plugin.py`. Discovery: entry points group `pyclaw.channels` or explicit `plugins.channels` list in config. Each plugin gets a `GatewayHandle` for dispatching inbound messages and sending outbound replies.

### Hook System

Hooks fire on gateway events (`gateway:startup`, `message:received`, `command:reset`, etc.). Bundled hooks: `session-memory` (writes conversation history to memory on reset), `boot-md` (injects `MEMORY.md` into agent context at startup). Custom hooks are Python scripts registered in config.

### fastagent.config.yaml

FastAgent reads this from CWD or `~/.pyclaw/`. It defines MCP server connections: `pyclaw` (HTTP, port 8081), `fetch`, `time`, `filesystem`. The gateway injects `X-Agent-Name` into the `pyclaw` server headers so tools can identify the calling agent.

### Installation, Updates, and Removal

pyclaw is distributed as a `uv tool` installed directly from the private GitHub repo over SSH. All install/update/remove operations require SSH access to GitHub (`git@github.com:jondecker76/pyclaw.git`). The SSH key is stored at `~/.ssh/pyclaw_github` with a Host entry in `~/.ssh/config`.

**First-time install** — requires `uv` and SSH access to GitHub:
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jondecker76/pyclaw/main/install.sh)
```
`install.sh` checks for `uv` (installs it if missing), finds the latest stable tag via `git ls-remote`, then runs `uv tool install`. After install, run `pyclaw init` to create `~/.pyclaw/config.yaml`.

Optional install flags:
```bash
bash install.sh --beta             # install latest from main instead
bash install.sh --version 0.2.0   # install a specific version
```

**Updates** — run from the installed `pyclaw` binary:
```bash
pyclaw update                   # latest stable tagged release
pyclaw update --beta            # latest commit from main (unstable)
pyclaw update --version 0.2.0   # specific version
```
Updates never touch `~/.pyclaw/` — config, sessions, memory, and jobs are always preserved.

**Removal:**
```bash
pyclaw uninstall          # removes the binary; prompts whether to delete ~/.pyclaw/
pyclaw uninstall --purge  # removes binary + ~/.pyclaw/ without prompting
```

### Release Workflow

Versioning is managed by `hatch-vcs` — the version is derived automatically from the git tag at install/build time. **Never manually edit the version in `pyproject.toml` or `__init__.py`.** The generated `pyclaw/_version.py` is gitignored.

To cut a release:
```bash
git tag v0.2.0
git push origin v0.2.0
gh release create v0.2.0 --title "v0.2.0" --notes "..." --latest
```

Tag format must be `vMAJOR.MINOR.PATCH`. The `pyclaw update` stable path uses `git ls-remote --tags --sort=-v:refname` to find the latest tag — it only matches tags of this exact format (no pre-release suffixes). Pre-release tags (e.g. `v0.2.0-beta.1`) are ignored by `pyclaw update` stable but reachable via `pyclaw update --version 0.2.0-beta.1`.

### Testing Patterns

```python
# Gateway stub for unit tests — skip __init__
gw = Gateway.__new__(Gateway)
gw._seen_message_ids = {}
gw._dedup_ttl_seconds = 60
gw._usage = {"messages": 0, "tokens": 0}

# Mock concurrency manager in AgentRunner tests
with patch("pyclaw.core.concurrency.get_manager"):
    ...

# Config schema tests use model_validate with camelCase keys
config = ExecApprovalsConfig.model_validate({"mode": "allowlist"})
```

`pytest.ini_options` sets `asyncio_mode = "auto"` — all async tests run without `@pytest.mark.asyncio`.
