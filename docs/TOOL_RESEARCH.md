# Tool Context Research

Research into on-demand tool discovery and context management for pyclawops agents.
The core problem: LLM providers require tool schemas in every request — sending all
schemas on every call burns tokens and scales poorly.

---

## Key Findings: FastAgent Internals

- `list_tools()` is called **once per `generate()` call** (once per user message),
  before the tool-use loop — confirmed in `augmented_llm_anthropic.py:213`
- Between user messages: tool list IS re-fetched → dynamic injection is possible
- Within a turn (tool-use loop): tool list is fixed
- **FastMCP registers tools at import time** — tool list is static, cannot change
  at runtime without a custom low-level MCP server
- `allowed_tools` config in `MCPServerSettings` filters schemas before LLM sees them
- FastAgent has no built-in schema compression or lazy loading

## Key Findings: OpenClaw

- Sends filtered tool schemas (not all tools) via a multi-layer policy pipeline:
  profile allowlists → agent-specific allow/deny → provider-specific → sandbox
- Manages token cost via **context result pruning** (trims old tool *results*),
  not schema pruning
- No semantic/dynamic tool selection — purely syntactic filtering
- Has a "skills" concept (workspace/home/bundled `.md` files)

## Prototype: exec_tool proxy (`/tmp/tool-proxy-test/`)

Tested 3-tool proxy pattern against MiniMax M2.5. Results:

```
get_tool_categories()             → {jobs: "...", filesystem: "...", ...}
get_tools_by_category(category)   → full arg schemas for that category
exec_tool(tool_name, args_json)   → dispatch to real implementation
```

**Results (3/3 test cases, MiniMax M2.5):**
- Protocol followed correctly every time
- Consistent pattern: always 4 turns (categories → tools → exec → respond)
- +3 extra turns per task vs direct tool calls
- Upfront token cost: ~254 tokens (3 schemas) vs ~347 tokens (5 tools, small set)
  — savings grow significantly at scale (30+ tools)
- Argument construction from schema descriptions: correct in all cases

**Dynamic injection variant tested:**
- FastMCP cannot expose new tools after state change (import-time registration)
- A custom low-level MCP server (not using FastMCP decorators) CAN return a
  different tool list per `list_tools()` call — would enable native tool calls
  after a discovery step, eliminating exec_tool blindness

---

## Options Catalogue

### Option 1: All tools in all context (current)
Send every tool schema on every request.
- Extra turns: 0 | Token cost: High, fixed | Complexity: None
- Breaks down above ~30 tools; provider limits may apply

### Option 2: exec_tool proxy (prototyped)
3 meta-tools: `get_tool_categories`, `get_tools_by_category`, `exec_tool`.
LLM discovers tools lazily via a 3-step protocol.
- Extra turns: +3/task | Token cost: Low upfront | Complexity: Low
- Risk: argument blindness (no JSON schema enforcement on exec_tool args)
- Mitigation: return full arg schemas in `get_tools_by_category` response body
- Works well at Claude/GPT-4 tier; smaller models may skip discovery

### Option 3: Semantic tool retrieval (tool RAG)
Before each `generate()`, embed the user message, cosine-compare against
embedded tool descriptions, inject only top-k most relevant schemas.
- Extra turns: 0 | Token cost: Low, dynamic | Complexity: Medium
- Requires local embedding model (nomic-embed, etc. — fast and small)
- Transparent to user and agent — just works
- Probabilistic: may miss tools for novel queries
- Shines at 50+ tools; overkill for small sets

### Option 4: Intent-based MCP server selection
Split into small domain MCP servers: `pyclawops-base`, `pyclawops-jobs`, `pyclawops-config`,
`pyclawops-memory`. Before calling FastAgent, pyclawops scans the message with
keyword/regex heuristics and selects which servers to attach.
- Extra turns: 0 | Token cost: Low, dynamic | Complexity: Low-Medium
- Deterministic, transparent, no new dependencies
- Selection logic lives in Python (AgentRunner), not the LLM
- Keyword matching covers ~90% of cases; edge cases need fallback

### Option 5: Session-level tool caching
On first encounter with a category (via exec_tool proxy), cache those schemas
in session context as a synthetic message. Subsequent turns in the same session
skip rediscovery entirely.
- Extra turns: +3 first time, 0 thereafter | Token cost: Low | Complexity: Low
- Naturally composable with Option 2
- Session cache grows as agent explores different tool domains

### Option 6: Compressed tool signatures
Skip JSON schemas entirely. Describe tools in system prompt as compact signatures:
```
create_job(name, schedule, agent, message) - creates a scheduled cron job
list_jobs() - lists all scheduled jobs
```
exec_tool dispatcher handles validation. LLM uses natural language understanding
for argument construction.
- Extra turns: 0 | Token cost: Very low | Complexity: Low
- Trades reliability for efficiency
- Works well for simple tools, degrades for complex argument structures

### Option 7: Sub-agent router (FastAgent native)
Use FastAgent's `LLMRouter` to route each message to a specialized sub-agent.
Router has zero tools (just sub-agent descriptions). Each sub-agent has a small
focused tool set (jobs-agent, fs-agent, memory-agent, etc.).
- Extra turns: +1 (routing call) | Token cost: Low per agent | Complexity: Medium
- Idiomatic FastAgent approach
- Routing call is cheap (classification only)
- LLMRouter or EmbeddingRouter both available in FastAgent

### Option 8: Skill chains / high-level operations
Expose composed operations rather than individual tools:
`schedule_daily_report`, `setup_monitoring`, `store_preference`.
Skills are pre-wired sequences; the LLM sees 5-10 high-level names instead of 30
low-level schemas. Skills execute multiple underlying tools internally.
- Extra turns: 0 | Token cost: Very low | Complexity: High (upfront authoring)
- Closest to OpenClaw's skills concept
- Skill library grows independently of what LLM reasons about

### Option 9: Dynamic MCP server (custom, not FastMCP)
Custom low-level MCP server (bypassing FastMCP decorators) maintains session state.
`load_category("jobs")` updates internal state; next `list_tools()` call returns
jobs schemas natively. LLM can call `create_job(...)` directly — no exec_tool.
- Extra turns: +1 first time, 0 thereafter | Token cost: Low | Complexity: High
- Best of both worlds: lazy loading + native tool calls + real schema validation
- Requires implementing MCP protocol directly (not FastMCP)

---

## Graph-Based Tool Knowledge (ClawVault / Obsidian)

### Why graph over RAG for tool selection

RAG answers: *"what tool descriptions are semantically similar to this query?"*
Graph answers: *"given this concept, what tools are related, and how?"*

For a curated, stable tool set (30-50 tools), explicit graph structure is
**architecturally superior** to pure RAG because:
- Tool relationships are first-class information (not encoded in vectors)
- Navigation is deterministic and explainable
- Concept → tool mappings can be explicitly authored
- Workflow/skill chains stored as graph paths
- Graph improves with agent experience (self-annotating)

### What the graph enables

**Concept aliases / intent vocabulary (explicit)**
```
"remind me"  → scheduling → [create_job, heartbeat]
"automate"   → scheduling → [create_job]
"remember"   → memory     → [memory_store]
"save"       → memory OR filesystem  (ambiguous — return both)
```

**Tool relationships**
```
create_job
  → see_also: [list_jobs, delete_job]
  → related_concept: [heartbeat, scheduling]
  → prerequisite: agent must exist in config
  → example: "run at 9am daily" → schedule: "0 9 * * *"
```

**Workflow nodes (skill chains)**
```
workflow: daily_briefing_setup
  trigger: ["set up daily briefing", "morning summary"]
  steps: [create_job("briefing", "0 8 * * *", ...), memory_store("prefs", ...)]
```

### ClawVault as the storage layer

Structure: one markdown file per tool + concept/category nodes with `[[wikilinks]]`.

```
vault/
  tools/
    create_job.md       ← description, args, examples, [[scheduling]] [[jobs]]
    list_jobs.md
    memory_store.md
  categories/
    jobs.md             ← [[create_job]] [[list_jobs]] [[delete_job]]
    memory.md
  concepts/
    scheduling.md       ← maps user intent to [[jobs]] and [[heartbeat]]
  workflows/
    daily_briefing.md
```

**Key advantage**: the agent already has memory tools that read/write ClawVault.
Tool knowledge is just another part of agent memory — the agent can:
- Read tool docs to understand capabilities
- Write annotations from experience ("used create_job for 'set reminder' — worked")
- Correct its own tool knowledge from user feedback

This enables **self-improving tool knowledge** without retraining.

### The cold-start / traversal problem

Challenge: going from raw user message → graph node still needs a lookup mechanism.
Options:
1. Keyword extraction → concept node (fast, deterministic, covers known vocabulary)
2. Embeddings on concept nodes (not tool nodes) — few nodes, well-described,
   then graph traversal from there. Hybrid of RAG + graph.
3. LLM call for concept extraction (expensive but highest quality)

### Honest downsides

- Upfront curation cost (mitigated if agent helps maintain the graph)
- Needs to ship a default tool graph with the package
- Maintenance burden grows with tool count (but agent can help)
- Pure graph struggles with novel/unexpected user phrasings

---

## Recommendation for Pyclaw

**Short term (low effort, immediate token savings):**
Option 4 (intent-based MCP server selection) — split the pyclawops MCP server into
domain servers, select which to attach per-message in AgentRunner. Pure Python,
no protocol changes, works today.

**Medium term (best balance):**
Option 4 + Option 5 (intent selection + session caching) — selection narrows the
set, caching means it's free after the first turn in each domain.

**Long term (best architecture):**
Graph-based tool knowledge in ClawVault + Option 9 (dynamic MCP server).
The tool graph serves as both the selection mechanism and agent self-documentation.
Agent annotates the graph from experience. Tool knowledge improves over time.

**For self-knowledge specifically (separate from tool selection):**
Static system prompt injection for core concepts + ClawVault graph for detailed
schemas + MCP tools for live operational data (current config, current jobs).

---

## Open Questions

1. Does ClawVault exist as a real implementation or is it a config placeholder?
2. How large does the tool set realistically get? (determines urgency)
3. Is MiniMax reliable enough for the exec_tool discovery protocol?
   (tested at 3/3 but small sample)
4. Would a local embedding model (nomic-embed via ollama) be acceptable
   as a dependency for Option 3?
5. Can FastAgent's EmbeddingRouter be used for Option 7 with a local model?

---

*Research session: 2026-03-09*
*Prototype: `/tmp/tool-proxy-test/` (proxy_server.py, test_protocol.py, test_dynamic_fastagent.py)*
