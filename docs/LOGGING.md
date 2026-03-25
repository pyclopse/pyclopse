# Logging

pyclawops uses a two-tier logging architecture: a broad gateway log for infrastructure events and a per-agent log for every conversation turn and tool call.

## Log files

| File | What it captures |
|------|-----------------|
| `~/.pyclawops/logs/pyclawops.log` | Gateway-level events: startup, API calls, job scheduling, session lifecycle, errors |
| `~/.pyclawops/agents/{agent_id}/logs/agent.log` | Every conversation turn and every MCP tool call for that agent |

Both files rotate daily at midnight. Rotated files are named `pyclawops-YYYY-MM-DD.log` and `agent-YYYY-MM-DD.log` respectively. Retention is controlled by `gateway.log_retention_days` in `config.yaml` (default: 7 days).

## Session prefixes

Every line in `agent.log` is prefixed with `[agent_id-XXXXXX]` where `XXXXXX` is the random suffix of the session ID (e.g. `[ritchie-W2RSQQ]`). This lets you distinguish:

- **Interactive sessions** — the user's active Telegram/Slack session
- **Job sessions** — isolated sessions created by the job scheduler (`channel=job`)

Both write to the same `agent.log` file, interleaved but always identifiable by session prefix.

Example:
```
2026-03-13 10:25:01,412 INFO     [ritchie-W2RSQQ] [STREAM] prompt: What's the market doing?…
2026-03-13 10:25:03,881 INFO     [ritchie-W2RSQQ] [TOOL] web_search(query='SPY QQQ today')
2026-03-13 10:25:04,201 INFO     [ritchie-W2RSQQ] [TOOL] web_search → SPY -0.8% QQQ -1.1%…
2026-03-13 10:25:08,774 INFO     [ritchie-W2RSQQ] [STREAM] completed
2026-03-13 10:55:00,003 INFO     [ritchie-JOBABC] [TURN] prompt: Run the trading scan…
2026-03-13 10:55:00,441 INFO     [ritchie-JOBABC] [TOOL] bash(command='uv run…scanner.py')
2026-03-13 10:55:45,119 INFO     [ritchie-JOBABC] [TOOL] bash → ## Market Overview…
2026-03-13 10:55:48,330 INFO     [ritchie-JOBABC] [TURN] response: The scan is complete…
```

## Log entry types

### Conversation turns (`runner.py`)

| Prefix | When |
|--------|------|
| `[TURN] prompt:` | Non-streaming send — prompt going into the agent |
| `[TURN] response:` | Non-streaming send — full response returned |
| `[STREAM] prompt:` | Streaming send — prompt going into the agent |
| `[STREAM] completed` | Streaming send — stream finished successfully |

Prompts and responses are truncated to 500 characters in the log (with `…` if cut off).

### Tool calls (FastMCP middleware in `tools/server.py`)

| Format | When |
|--------|------|
| `[TOOL] tool_name(arg=val, …)` | Before the tool executes |
| `[TOOL] tool_name → result preview…` | After the tool returns (300 char preview) |
| `[TOOL] tool_name raised ExcType: msg` | If the tool raises an exception (WARNING level) |

Tool argument values are truncated to 120 characters each. Tool results are truncated to 300 characters with newlines collapsed to spaces.

## How it works

### Gateway log filter (`__main__.py`)

`_ExcludeAgentDetailFilter` is attached to the `pyclawops.log` file handler. It suppresses `pyclawops.agent.*` records at INFO and DEBUG level so per-agent chatter doesn't pollute the gateway log. WARNING and above from agent loggers still appear in both files.

### Per-agent log setup (`__main__.py`)

`setup_agent_logging(agent_id, agents_dir, retention_days)` is called once per agent after `gateway.initialize()`. It attaches a `TimedRotatingFileHandler` to the `pyclawops.agent.{agent_id}` logger. The handler is idempotent — calling it twice for the same agent is safe.

### Session ID propagation

Each `AgentRunner` receives a `session_id` at construction time (`agent.py: _get_session_runner()`). This ID is:

1. Used to compute `_log_prefix` (`[ritchie-W2RSQQ]`) for all log lines emitted by that runner.
2. Injected as the `X-Session-ID` HTTP header into the pyclawops MCP server connection alongside `X-Agent-Name`. The `_ToolLoggingMiddleware` reads both headers to route tool call logs to the right logger with the right prefix.

### Tool logging middleware (`tools/server.py`)

`_ToolLoggingMiddleware` is registered on the FastMCP server at startup via `mcp.add_middleware()`. It overrides `on_call_tool`, which is called for every tool invocation regardless of which tool it is — no per-tool changes required. Agent name and session ID come from HTTP request headers set by the calling `AgentRunner`.

## Silencing FastAgent's console output

FastAgent has a rich terminal renderer that prints colorful markdown, tool call displays, and streaming progress to stdout. This output conflicts with pyclawops's own logging and the terminal UI.

The correct way to disable it is the `quiet=True` parameter on the `FastAgent()` constructor:

```python
fast = FastAgent(self.agent_name, quiet=True, parse_cli_args=False)
```

`quiet=True` modifies the config dict before any display objects are created and explicitly stops the progress display. It sets `show_chat=False`, `show_tools=False`, and `progress_display=False` in the live settings object.

**Why `fastagent.config.yaml` alone is not enough:**

FastAgent reads `fastagent.config.yaml` from the current working directory (walking up to parents). When pyclawops is run from a directory that already contains a `fastagent.config.yaml` (e.g. the project root during development), that file takes precedence over `~/.pyclawops/fastagent.config.yaml`. Any logger settings in the user config are silently ignored.

**Why `update_global_settings()` alone is not enough:**

`update_global_settings()` replaces the global `_settings` singleton, but FastAgent reads and caches settings during `FastAgent.__init__()` before `update_global_settings()` has a chance to run in a typical call sequence.

`quiet=True` is the only approach that is guaranteed to work regardless of which config file is found or when settings are read.

`parse_cli_args=False` is also set to prevent FastAgent from trying to parse `sys.argv`, which would conflict with pyclawops's own argument parser.
