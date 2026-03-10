# AGENTS.md — pyclaw Coding Assistant Context

> For AI coding assistants (Claude Code, Cursor, Copilot, etc.) working on this codebase.

## What Is pyclaw?

pyclaw is an open-source Python AI gateway — a self-hosted personal AI assistant server.
It routes messages from Telegram/Slack → an LLM agent → back to the user, with support for:
- Multiple messaging channels (Telegram, Slack, plugins)
- Multiple LLM providers (OpenAI, MiniMax, any OpenAI-compatible endpoint)
- Session management, cron jobs, memory, MCP tools, skills
- A terminal UI (TUI) for local use

## Project Layout

```
pyclaw/
  core/
    gateway.py          # Main orchestrator — channels, jobs, sessions, agents
    agent.py            # AgentManager: creates FastAgent instances
    commands.py         # CommandRegistry: /help /reset /status /model /job
    session.py          # SessionManager: persist + reap sessions
    concurrency.py      # Per-model concurrency rate limiting
    prompt_builder.py   # Builds system prompts from ~/.pyclaw/agents/<name>/ files
    router.py           # Routes inbound messages to correct handler
    templates/          # Default bootstrap file templates (AGENTS.md, SOUL.md, etc.)
  config/
    schema.py           # Pydantic config models (Config, AgentConfig, SlackConfig, …)
    loader.py           # Load + validate config.yaml, resolve ${env:X} secrets
  channels/
    telegram.py         # Telegram channel adapter
    slack.py            # Slack channel adapter
    loader.py           # Dynamic plugin loader for channel adapters
  agents/
    runner.py           # AgentRunner: wraps FastAgent, strips <thinking> tags
  jobs/
    scheduler.py        # JobScheduler: cron jobs with notify_callback
  memory/
    file_backend.py     # FileMemoryBackend: daily journals + vector index
    embeddings.py       # EmbeddingBackend ABC + provider implementations
  tools/
    server.py           # FastMCP server: memory_read/write/search/reindex, etc.
  skills/
    registry.py         # Discover + load skill files (SKILL.md)
  hooks/
    engine.py           # Hook engine: event-driven script execution
    bundled/            # Built-in hooks (session-memory, boot-md)
  tui/
    screens.py          # Textual TUI screens
  __main__.py           # CLI entry point: `python -m pyclaw`
examples/
  config.yaml           # Fully documented example configuration
tests/                  # pytest test suite
```

## Key Conventions

### Config Schema
- Lives in `pyclaw/config/schema.py` — Pydantic v2 models
- Uses `validation_alias=AliasChoices("snake_case", "camelCase")` for YAML keys
- Test with `Model.model_validate({"camelCase": val})`, not `Model(snake_case=val)`

### Testing
- Run: `uv run pytest`
- Always use `uv`, never `python3 -m pytest` or `.venv/bin/pytest`
- Gateway unit tests use `Gateway.__new__(Gateway)` to skip `__init__`
- Stubs need: `_seen_message_ids = {}`, `_dedup_ttl_seconds = 60`, `_usage = {...}`
- Mock concurrency: `patch("pyclaw.core.concurrency.get_manager")`

### Gateway Internals
- `_init_channels()` — sets up Telegram bot + Slack AsyncWebClient
- `_init_pulse()` — builds `PulseRunner` with heartbeat executor
- `_slack_web_client` — `AsyncWebClient` stored on gateway for outbound Slack messages
- `_telegram_bot` / `_telegram_chat_id` — stored on gateway for Telegram

### Memory System
- `FileMemoryBackend`: writes JSON to `~/.pyclaw/agents/<name>/memory/<key>.json`
- `reindex(batch_size)` rebuilds vector index in `memory/vectors.json`
- `MemoryService` wraps `FileMemoryBackend` — access inner backend via `svc._default`
- MCP tool `memory_reindex` unwraps `MemoryService → FileMemoryBackend` before reindexing

### Bootstrap / System Prompt
- `prompt_builder.py` builds the FastAgent system prompt from files in `~/.pyclaw/agents/<name>/`
- Files loaded (in order): AGENTS.md, PERSONALITY.md, IDENTITY.md, RULES.md, USER.md, PULSE.md, SOUL.md, HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md
- Templates for new agents live in `pyclaw/core/templates/` — `ensure_agent_files()` copies them
- System prompt is always present (not re-injected after compaction)

### Inline Secrets
- Config supports `${env:VAR}`, `${keychain:Name}`, `${file:~/.secret}`
- Resolved at load time in `config/loader.py`

## What's NOT Here

- No web UI (use TUI or messaging channels)
- No multi-user auth (single-user personal assistant)
- No database (flat files + JSON)

## Making Changes

1. Config schema changes → update `pyclaw/config/schema.py` + `examples/config.yaml`
2. New MCP tools → add to `pyclaw/tools/server.py`
3. New channels → implement adapter + register in `pyclaw/channels/loader.py`
4. New slash commands → add to `CommandRegistry` in `pyclaw/core/commands.py`
5. Gateway features → `pyclaw/core/gateway.py` (keep `__init__` minimal; use `_init_*` methods)
