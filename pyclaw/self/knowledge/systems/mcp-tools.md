# MCP Tools Server

**File:** `pyclaw/tools/server.py`
**Port:** 8081
**Library:** FastMCP

The pyclaw MCP server exposes pyclaw-native tools to FastAgent. It runs as a
managed asyncio task started before `gateway.initialize()`. FastAgent connects
to it eagerly during agent startup.

---

## Server Lifecycle

```python
# Start (in gateway.start_mcp_server)
await pyclaw_mcp.run_http_async(host=host, port=port, show_banner=False)

# Stop (in gateway.stop_mcp_server)
self._mcp_server_task.cancel()
# noisy loggers silenced before cancel, restored after
```

**Never** replace `run_http_async` with direct uvicorn calls or
`mcp.http_app()` + manual uvicorn. FastMCP owns the entire server lifecycle.

---

## Tool Logging Middleware

`_ToolLoggingMiddleware` wraps the FastMCP app. It reads the `X-Agent-Name`
and `X-Session-Id` headers injected by the gateway, and writes a log entry to
the per-agent logger for every tool call. The Gateway injects `X-Agent-Name`
into the MCP server's HTTP headers so tools can identify which agent is calling.

---

## All Exposed Tools

### Session Tools
| Tool | Description |
|------|-------------|
| `sessions_list()` | List active sessions |
| `sessions_history(session_id)` | Conversation history for a session |
| `sessions_send(session_id, message)` | Inject message into another session |
| `sessions_spawn(agent, task, ...)` | Spawn a subagent session |

### Memory Tools
| Tool | Description |
|------|-------------|
| `memory_store(key, content, tags?)` | Store a memory entry |
| `memory_get(key)` | Get entry by key |
| `memory_search(query, limit?)` | Search (keyword or vector) |
| `memory_list(prefix?)` | List keys |
| `memory_delete(key)` | Delete entry |
| `memory_reindex()` | Rebuild vector index |

### Job Tools
| Tool | Description |
|------|-------------|
| `job_list()` | List all jobs |
| `job_get(name)` | Get job by name/ID |
| `job_create(...)` | Create a new job |
| `job_update(name, ...)` | Update job fields |
| `job_delete(name)` | Delete a job |
| `job_enable/disable(name)` | Enable or disable |
| `job_run_now(name)` | Trigger immediately |
| `job_history(name)` | Get run history |

### Config Tools
| Tool | Description |
|------|-------------|
| `config_get()` | Get config (secrets redacted) |
| `config_set(path, value)` | Set value by dot-notation path |
| `config_delete(path)` | Delete a config value |
| `config_validate()` | Validate current config |
| `config_reload()` | Reload from disk |
| `config_schema(section?)` | Pydantic schema for a section |

### Subagent Tools
| Tool | Description |
|------|-------------|
| `subagent_spawn(task, ...)` | Spawn background subagent |
| `subagents_list()` | List active subagents |
| `subagent_status(job_id)` | Subagent details |
| `subagent_kill(job_id)` | Cancel subagent |
| `subagent_interrupt(job_id, task)` | Kill and respawn |
| `subagent_send(job_id, message)` | Queue follow-up message |

### Utility Tools
| Tool | Description |
|------|-------------|
| `bash(command)` | Shell execution with security policy |
| `web_search(query)` | DuckDuckGo search |
| `send_message(channel, text, ...)` | Send to a channel |
| `agents_list()` | List configured agents |
| `session_status()` | Current session metadata |
| `image(path_or_url, prompt?)` | Vision model understanding |
| `tts(text, ...)` | MiniMax text-to-speech |
| `process(action, pid?)` | List/kill background processes |
| `audit_log_tail(lines?)` | Recent audit log entries |
| `audit_log_search(query)` | Search audit log |

---

## Tool Patterns

### HTTP Error Formatting

When a tool receives a 404 from the REST API, use `_fmt_http_err(e, resource_id)`:

```python
def _fmt_http_err(e: httpx.HTTPStatusError, resource_id: str) -> str:
    if e.response.status_code == 404:
        return f"[NOT FOUND] {resource_id}"
    return f"[ERROR] HTTP {e.response.status_code}: {e.response.text}"
```

Never return raw exception strings to the agent.

### Internal REST Calls

Tools call the gateway's REST API via helper functions:
```python
def _jobs_api() -> str:   return "http://localhost:8080/api/v1/jobs"
def _memory_api() -> str: return "http://localhost:8080/api/v1/memory"
def _config_api() -> str: return "http://localhost:8080/api/v1/config"
```

---

## bash Tool Security

The `bash` tool enforces `ExecApprovalSystem` policy:

| Mode | Behaviour |
|------|-----------|
| `allowlist` | Only binaries in `safe_bins` are permitted |
| `denylist` | All binaries except `deny_list` are permitted |
| `all` | All commands permitted |
| `none` | All commands denied |
