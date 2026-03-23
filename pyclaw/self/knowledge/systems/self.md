# Self-Knowledge System

**Files:** `pyclaw/self/loader.py`, `pyclaw/self/knowledge/`,
`pyclaw/api/routes/self_.py`
**Served via:** pyclaw MCP server (port 8081) — no separate server

The self-knowledge system gives agents access to pyclaw's own documentation
and source code over MCP. Agents can read architecture docs, system guides,
and live source files — enabling self-aware, architecture-informed behaviour.

---

## MCP Tools (port 8082)

Agents connect via `self` in their `mcpServers` list.

| Tool | Signature | Purpose |
|------|-----------|---------|
| `self_topics` | `() → str` | List all available knowledge topics |
| `self_read` | `(topic: str) → str` | Read a documentation topic |
| `self_source` | `(module: str) → str` | Read pyclaw source with line numbers |

### Usage pattern

```python
# Discover topics first
self_topics()

# Read a topic by path (no .md extension)
self_read('overview')
self_read('architecture/gateway')
self_read('systems/jobs')
self_read('development/conventions')

# Read source code (path relative to pyclaw package)
self_source('core/gateway.py')
self_source('agents/runner.py')
self_source('tools/server.py')
```

---

## REST API (port 8080)

The same data is also exposed over the REST API for external clients that
cannot connect to MCP:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/self/topics` | List topics |
| `GET` | `/api/v1/self/topic/{path}` | Read a topic |
| `GET` | `/api/v1/self/source/{module}` | Read source |

---

## Knowledge Base

Topics live in `pyclaw/self/knowledge/` and are served by `DocLoader`.
`index.md` is the root listing. Each topic is a plain markdown file.

```
pyclaw/self/knowledge/
├── index.md          ← topic registry (self_topics() returns this)
├── overview.md       ← request flow, startup, key files
├── architecture/     ← gateway, sessions, agents, channels, hooks, queue
├── systems/          ← jobs, memory, skills, security, config, mcp-tools,
│                        api, tui, a2a, workflows, self (this file)
└── development/      ← testing, conventions, extending, release
```

`DocLoader.read(topic)` returns `[NOT FOUND]` for missing topics and
`[ERROR]` for I/O errors. Both prefixes are checked by the REST API to
return 404 or 400 respectively.

---

## Config

No separate config needed. The tools are part of the pyclaw MCP server (8081)
and available to any agent that has `pyclaw` in its `mcpServers` — which is
every agent by default.

```yaml
agents:
  my_agent:
    useFastagent: true
    mcpServers:
      - pyclaw      # ← self_topics/self_read/self_source are included here
      - fetch
      - time
      - filesystem
```
