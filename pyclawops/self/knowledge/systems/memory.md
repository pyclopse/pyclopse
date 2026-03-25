
# Memory System

**Files:** `pyclawops/memory/service.py`, `pyclawops/memory/file_backend.py`,
`pyclawops/memory/backend.py`, `pyclawops/memory/embeddings.py`,
`pyclawops/memory/clawvault.py`

---

## Architecture

```
Agent → MCP tool (memory_store, memory_search, ...)
    → MemoryService.write/read/search/...
        → HookRegistry.intercept(memory:write, ...)
            → plugin handler (if registered) OR
            → FileMemoryBackend (default)
```

`MemoryService` routes all operations through the hook system. This means any
plugin can transparently replace the backend by registering an intercept handler
for `memory:*` events. The default backend is `FileMemoryBackend`.

---

## FileMemoryBackend (`pyclawops/memory/file_backend.py`)

### Daily Journals

Per-agent daily markdown files at:
```
~/.pyclawops/agents/{agent_id}/memory/YYYY-MM-DD.md
```

Each entry is a section with key, content, tags, and separator:
```markdown
## my-key

The stored content goes here.

Tags: tag1, tag2
---
```

Entries are appended; deletion marks the entry with a `_deleted: true` flag.
The file grows over time; reindex rebuilds a clean version.

### MEMORY.md (curated)

```
~/.pyclawops/agents/{agent_id}/memory/MEMORY.md
```

This file is **user-edited, never written by tools**. It contains curated
long-term context that the user wants the agent to always have. It is injected
into the agent's system prompt via the `boot-md` hook at startup and via
`include_memory` in job prompts.

Do not use `memory_store()` to write to MEMORY.md. That tool writes to the
daily journal. MEMORY.md is edited by the user directly.

### Search

Keyword search by default: scans entry content for the query string.

When `memory.embedding.enabled: true`, semantic vector search is available.
Entries are embedded on write and stored in `memory/vectors.json`. Queries
are embedded and ranked by cosine similarity.

---

## Vector Search (`pyclawops/memory/embeddings.py`)

`EmbeddingBackend` ABC with implementations:
- `OpenAIEmbeddingBackend` — OpenAI `text-embedding-3-small` (or configured)
- `GeminiEmbeddingBackend` — Gemini embedding API
- `LocalEmbeddingBackend` — Ollama or llama.cpp via OpenAI-compat endpoint

Config:
```yaml
memory:
  backend: file     # file | clawvault
  embedding:
    enabled: true
    provider: openai   # openai | gemini | local
    model: text-embedding-3-small
```

`memory_reindex` MCP tool rebuilds `vectors.json` from scratch — run after
bulk imports or if the index gets out of sync.

---

## ClawVault (`pyclawops/memory/clawvault.py`)

Alternative backend with a key-value store and optional vector search.
Configured via `memory.backend: clawvault`. Used when more structured
storage is needed than the daily journal format.

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `memory_store(key, content, tags?)` | Store a new entry |
| `memory_get(key)` | Get entry by key |
| `memory_search(query, limit?)` | Search entries (keyword or vector) |
| `memory_list(prefix?)` | List all keys |
| `memory_delete(key)` | Delete an entry |
| `memory_reindex()` | Rebuild vector index |

All tools call the REST API which calls `MemoryService` which routes through
the hook registry.
