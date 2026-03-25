# A2A — Agent-to-Agent Protocol

**Files:** `pyclawops/a2a/executor.py`, `pyclawops/a2a/setup.py`
**Spec:** Google A2A Protocol (JSON-RPC over HTTP)
**Dependency:** `a2a-sdk` (optional — A2A endpoints are disabled if not installed)

A2A (Agent-to-Agent) is a protocol that lets external AI systems call pyclawops
agents directly over HTTP using a standardised JSON-RPC interface. When enabled,
each configured agent gets its own A2A endpoint that any A2A-compatible client
(another agent, orchestrator, or tool) can invoke.

---

## How It Works

```
External A2A client
    → POST /a2a/{agent_id}/   (JSON-RPC: tasks/send)
        → PyclawAgentExecutor.execute()
            → gateway.handle_message(channel="a2a", ...)
                → SessionManager → Agent → AgentRunner → FastAgent
            → event_queue.enqueue_event(response text)
        → response streamed back to A2A client
```

A2A routes are mounted onto the **existing FastAPI app** (port 8080) after
`gateway.initialize()`. They share the same process and port as the REST API.

---

## Endpoints (per agent)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/a2a/{agent_id}/.well-known/agent.json` | Agent card (capabilities, skills, metadata) |
| `POST` | `/a2a/{agent_id}/` | JSON-RPC endpoint (tasks/send, tasks/cancel, etc.) |
| `GET` | `/a2a/{agent_id}/agent/authenticatedExtendedCard` | Extended agent card |

---

## Agent Card

Each agent's card is auto-generated from config and includes:
- **name** — agent ID
- **description** — from `AgentConfig.description`
- **version** — pyclawops package version
- **capabilities** — streaming: false, push_notifications: true if Telegram/Slack configured
- **skills** — auto-populated from the agent's skill registry
- **transport** — JSON-RPC

---

## Session Modes

`session_mode` controls how A2A tasks map to pyclawops sessions:

| Mode | Behaviour |
|------|-----------|
| `shared` (default) | Routes into the agent's single active session — full conversation context, same session as Telegram/TUI messages |
| `isolated` | Each A2A task gets its own session (like a job) — no prior context, uses task_id as session key |

```yaml
agents:
  ritchie:
    a2a:
      enabled: true
      sessionMode: shared   # or isolated
```

---

## Config

### Global enable

```yaml
gateway:
  a2a:
    enabled: true
```

### Per-agent

```yaml
agents:
  ritchie:
    a2a:
      enabled: true
      allowInbound: true     # accept incoming A2A requests (default: true)
      allowOutbound: false   # allow this agent to call other agents via A2A client tools (default: false)
      sessionMode: shared    # shared | isolated (default: shared)
```

If `gateway.a2a.enabled` is false, **no** A2A endpoints are mounted regardless
of per-agent config. If an agent has no `a2a:` block, it is only mounted when
`gateway.a2a.enabled: true`.

---

## PyclawAgentExecutor (`pyclawops/a2a/executor.py`)

Bridges the A2A `AgentExecutor` interface to `gateway.handle_message()`.

- `execute(context, event_queue)` — extracts user input from the A2A request
  context, calls `gateway.handle_message()`, enqueues the response text
- `cancel(context, event_queue)` — cancellation is not yet implemented; logged
  and ignored

---

## mount_a2a_routes (`pyclawops/a2a/setup.py`)

Called in `gateway.initialize()` after the FastAPI app is created. Iterates
all configured agents, checks A2A config, builds the `AgentCard`, creates a
`PyclawAgentExecutor` + `DefaultRequestHandler`, and mounts the routes.

Returns the count of successfully mounted agents. Safe to call when `a2a-sdk`
is not installed — logs a warning and returns 0.

---

## Dependencies

```
pip install a2a-sdk
```

If `a2a-sdk` is not installed, `mount_a2a_routes` logs a warning and A2A is
silently disabled. All other pyclawops functionality is unaffected.

---

## Example: calling a pyclawops agent via A2A

```python
import httpx

# Discover the agent
card = httpx.get("http://localhost:8080/a2a/ritchie/.well-known/agent.json").json()

# Send a task
response = httpx.post(
    "http://localhost:8080/a2a/ritchie/",
    json={
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": "1",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "What's the weather today?"}]
            }
        }
    }
)
```
