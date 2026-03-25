# Vault Memory System

The vault is pyclawops's persistent long-term memory layer. It extracts durable facts from conversations and documents, stores them as typed, structured records, and automatically injects relevant context into agent prompts at query time. The system is designed to surface the right memories at the right moment — not dump everything into every prompt.

---

## Table of Contents

1. [Overview](#overview)
2. [Memory Types](#memory-types)
3. [Fact Structure](#fact-structure)
4. [Ingestion Pipeline](#ingestion-pipeline)
5. [Search and Retrieval](#search-and-retrieval)
6. [Context Injection](#context-injection)
7. [Deduplication and Reweaving](#deduplication-and-reweaving)
8. [Lifecycle Management](#lifecycle-management)
9. [Retrieval Profiles](#retrieval-profiles)
10. [Wikilinks and the Knowledge Graph](#wikilinks-and-the-knowledge-graph)
11. [Session-Level Seen-Fact Cache](#session-level-seen-fact-cache)
12. [Cursor Store and Crash Recovery](#cursor-store-and-crash-recovery)
13. [Configuration](#configuration)
14. [Storage Layout](#storage-layout)

---

## Overview

The vault sits between conversations and the agent's system prompt. After each conversation segment, a background extraction agent reads the transcript, identifies facts worth remembering, and writes them to disk as structured Markdown files. On every incoming message, the vault searches those facts and prepends the most relevant ones to the agent's prompt — invisibly, without polluting the session history.

```
Conversation → IngestionHandler → MemoryAgent (LLM) → VaultStore (disk)
                                                              ↓
User message → ContextAssembler → FallbackSearch/HybridSearch → injected <memory> block → Agent prompt
```

The vault is per-agent: each agent has its own isolated vault directory under `~/.pyclawops/agents/{agent_id}/vault/`.

---

## Memory Types

Every fact has a `type` that governs how it is classified, displayed, and retrieved. Types are defined in `pyclawops/memory/vault/models.py` as the `MemoryType` enum. Custom types can also be defined in `pyclawops.yaml` and are registered at startup via `TypeSchemaRegistry`.

### Built-in Types

| Type | Description | Example |
|------|-------------|---------|
| `user` | Personal identity and background facts about the user: job, role, skills, experience | "Senior backend engineer with 12 years of Python experience" |
| `preference` | Stable personal preferences about tools, style, or working method — lasting inclinations, not one-time choices | "Prefers uv over pip for package management" |
| `decision` | A specific choice made for a named project or initiative | "Decided to use PostgreSQL for AZDB" |
| `fact` | Contextual facts that don't fit a more specific type — use sparingly | "The AZDB repo lives on an external volume" |
| `lesson` | Something learned the hard way with lasting impact | "Mocked tests passed but prod migration failed — never mock the DB" |
| `commitment` | An explicit future action the user committed to | "Will migrate off Redis by end of Q2" |
| `goal` | A longer-horizon objective the user is working toward | "Plans to open-source AZDB by end of year" |
| `project` | A named ongoing project — acts as an anchor for related facts via `part_of` links | "AZDB: distributed key-value store for embedded use" |
| `context` | Environment and setup facts: OS, hardware, dev tools, language versions | "Runs macOS on Apple Silicon M3 MacBook Pro" |
| `person` | Information about a real person in the user's orbit | "Alex is the on-call lead for infrastructure at Acme" |
| `hypothesis` | Tentative or unconfirmed belief, typically low confidence (<0.6) | "Thinks the auth middleware may be causing latency — unconfirmed" |
| `absence` | Confirmed non-existence or deliberate non-use of something | "Does not use Kubernetes anywhere in the stack" |
| `anti` | Explicitly rejected option, often with stated reasoning | "Will not use MongoDB — relational queries were too painful" |
| `rule` | **Never auto-injected.** Behavioral constraint or mandate the user has imposed on the agent — governs agent behavior, not user facts | "Always check schema before editing config files" |

### The `rule` Type

`rule` facts are special: they are stored in the vault but **never automatically injected** into the prompt. The `_NO_INJECT_TYPES` frozenset in `retrieval.py` excludes them from all automatic context assembly. The agent must query them explicitly when needed (e.g., before performing a sensitive operation). This prevents rules from flooding every prompt while still making them queryable on demand.

### Custom Types

Custom types are defined in `pyclawops.yaml` under the `vault.types` list:

```yaml
vault:
  types:
    - name: stock_alert
      description: A stock price alert condition the user has set
      keywords: [stock, ticker, alert, threshold]
      color: "#f59e0b"
```

Custom types are passed to the extraction agent as part of the type list and are treated identically to built-in types in storage and retrieval.

---

## Fact Structure

Each fact is a `VaultFact` Pydantic model stored as a Markdown file with YAML frontmatter. The canonical fields are:

```yaml
---
id: 01KMGEGB56B8TV7YY1JHKDYF87        # ULID — monotonic, sortable
type: preference                        # MemoryType value or custom string
state: crystallized                     # provisional | crystallized | superseded | archived
claim: "Prefers 4-space indentation"    # One atomic fact statement
contrastive: "over 2-space"             # Optional "X over Y because Z" form
implied: false                          # True = inferred, not explicitly stated
confidence: 0.85                        # 0.0–1.0
reinforcement_count: 3                  # How many times seen again
surprise_score: 0.0                     # 0.0–1.0; high = agent was corrected
written_at: 2026-03-10T14:22:00Z
valid_until: null                       # Set when superseded
source_sessions:
  - session_id: 2026-03-10-abc123
    message_range: [4, 12]
source_file: null                       # Set if extracted from a document
supersedes: null                        # ULID of fact this replaced
superseded_by: null                     # ULID of fact that replaced this
related_to: []                          # ULIDs of related facts (generic)
depends_on: []                          # ULIDs this fact requires to be true
part_of: null                           # ULID of parent fact/project
contradicts: []                         # ULIDs of explicitly contradicted facts
tier: 1                                 # 1=full | 2=fact-only | 3=summary | 4=tags
---

Optional markdown body with narrative context and [[wikilinks]].
```

Facts are stored in `vault/facts/{ULID}.md`. Archived and superseded facts are moved to `vault/archive/{ULID}.md`.

---

## Ingestion Pipeline

Ingestion is handled by `IngestionHandler` (`pyclawops/memory/vault/ingestion.py`). It runs as a background task after each conversation segment and on a scheduled catch-up pass.

### Conversation Ingestion

1. **Cursor check** — The cursor store records how far into each session has been processed (`last_message_index`). Only unprocessed messages are ingested.
2. **Related facts search** — The last 3 messages are joined into a query and used to search the vault for existing related facts (up to 10). These are passed to the LLM as context.
3. **Memory agent call** — The extraction LLM receives the conversation transcript plus existing facts. It returns a JSON response with `create`, `reinforce`, or `supersede` actions.
4. **Processing** — Each extraction action is handled (see below).
5. **Cursor update** — The session cursor advances to the latest processed message index.

### Document Ingestion

Memory documents (Markdown files in `~/.pyclawops/agents/{agent_id}/memory/`) are also processed:

1. **Hash check** — The document's SHA-256 hash is compared to the stored hash. If unchanged, processing is skipped.
2. **Existing facts** — All facts with `source_file` matching this document are loaded and passed to the LLM as context.
3. **Memory agent call** — The LLM processes the document content (capped at 8000 characters).
4. **Cursor update** — The document cursor records the new hash and the IDs of all extracted facts.

### Catch-Up Pass

`run_catch_up()` scans all sessions and documents for unprocessed content and runs ingestion on each, oldest-first. This runs on startup and on a scheduled interval so no conversation is ever missed.

### Skip Channels

The `job` and `a2a` channels are always skipped during ingestion — automated outputs should not pollute the vault with machine-generated content.

---

## Search and Retrieval

The vault supports two search backends, configured via `vault.search.backend` in `pyclawops.yaml`.

### FallbackSearchBackend

A pure keyword search backend with no external dependencies. Stored in memory, rebuilt from disk on startup.

**Scoring:**
- Exact phrase match in claim: **+10**
- Each content word (stopwords stripped) found in claim: **+1 per word**
- Each content word found in body: **+0.5 per word**
- Quality bonus (high confidence + reinforcement): **+0–2**
- Score normalized by dividing by **16.0** → 0–1 scale

**Stopwords** — a frozenset of ~60 common words (articles, prepositions, conjunctions, common verbs, pronouns) is stripped from both query and fact before scoring. This prevents "what is X" style queries from matching everything that contains "is".

### HybridSearchBackend

Combines QMD semantic vector search with keyword scoring via **Reciprocal Rank Fusion (RRF)**:

```
rrf_score = 1/(k + rank_semantic) + 1/(k + rank_keyword)
```

The combined RRF score is normalized by the theoretical ceiling `(2 / (k+1)) × 2.25` → 0–1 scale. This makes the score meaningful and threshold-comparable regardless of collection size.

### Score Normalization

Both backends produce scores on a **0–1 normalized scale**. This is critical: the `min_relevance_score` config threshold is applied against these normalized scores, so `0.5` means the same thing regardless of backend.

---

## Context Injection

Context injection is handled by `ContextAssembler.assemble()` (`pyclawops/memory/vault/retrieval.py`), called from `Agent._prepend_vault_context()` (`pyclawops/core/agent.py`) on every incoming user message.

### Injection Guards

Before any search is performed, several gates are checked:

- **Channel skip** — `job` and `a2a` channels never receive vault context.
- **Allowed channels** — If `vault.agent.channels` is set, only listed channels receive context.
- **Word count guard** — Queries shorter than `min_query_words` (default: 3) skip injection, unless the single word is not a conversational filler word.
- **Single-word skip list** (`_RECALL_SKIP_WORDS`) — Single-word conversational tokens like "ok", "yes", "thanks", "lol" always skip injection regardless of config.

### Trigger-Based Score Multiplier

Before calling the assembler, the agent classifies the query's intent to adjust the effective injection threshold:

| Query type | Multiplier | Effect |
|------------|-----------|--------|
| Questions (`?`, WH-words: what/who/where/when/why/how/which, recall words: tell/explain/describe/remember/remind) | `×1.0` | Normal threshold — information requests welcome context |
| Task commands (first word is a command verb like fix/write/create/build/add/implement, no question signal) | `×0.75` | Raises effective bar — context less relevant for pure tasks |
| Ambiguous | `×0.9` | Slight raise |

The multiplier is applied to every candidate fact's score before the threshold comparison. This means a fact scoring 0.55 against a 0.5 threshold will still pass for questions (0.55 × 1.0 = 0.55 ≥ 0.5) but fail for commands (0.55 × 0.75 = 0.41 < 0.5).

### Keyword Boosting

After the search backend returns results, each fact's score is boosted based on how many content words from the query appear in the fact's claim or body:

```
match_count = number of query content words found in fact.claim or fact.body
boost = min(match_count × 0.15, 0.5)   # max +50%
score = score × (1.0 + boost)
```

This is a **pure boost** — there is no penalty for zero matches. Facts with no keyword overlap simply receive no boost and remain at their raw search score. Facts with 3+ matching words can receive up to a 50% score uplift, pushing them clearly above the threshold and ranking them above vaguer matches.

### Filtering

After scoring and boosting, facts are filtered:

- `state` must not be `superseded` or `archived`
- `valid_until` must be null or in the future
- `confidence` must be ≥ `confidence_threshold` (default: 0.5)
- Score must be ≥ `min_relevance_score` (default: 0.5)
- `rule` type facts are never included (excluded via `_NO_INJECT_TYPES`)

### Profile Ordering

The remaining facts are sorted by retrieval profile (see [Retrieval Profiles](#retrieval-profiles)) and capped at `injection_limit` (default: 5).

### Graph Expansion

After the main result set is assembled, BFS expansion follows fact links (`related_to`, `depends_on`, `part_of`) up to `graph_hops` hops (default: 2). Each hop applies a `0.85` score penalty. Linked facts are appended to fill any remaining slots up to `injection_limit`.

### Prompt Injection Format

Injected facts are prepended to the user's message as an XML block:

```xml
<memory>
  <fact type="preference">Prefers uv over pip for package management</fact>
  <fact type="decision">Redis used for caching in AZDB project (over Memcached because of persistence)</fact>
  <fact type="context">Runs macOS on Apple Silicon M3 MacBook Pro</fact>
</memory>
```

This block is invisible in the rendered chat — it only appears in the prompt the agent receives.

### `show_recall`

When `vault.show_recall: true` is set in config, a human-readable `<recall>` block is appended to the agent's reply (prepended, not saved to history) showing the user exactly which memories were injected and their confidence scores. Useful for debugging memory quality.

---

## Deduplication and Reweaving

The vault has a multi-layer deduplication system to prevent the same fact from being stored repeatedly as new conversations reinforce existing knowledge.

### Layer 1: LLM Context (Upstream)

Before the extraction agent is called, existing related facts are retrieved and passed as context. The LLM is explicitly instructed to `reinforce` or `supersede` existing facts rather than `create` duplicates. This is the first and most important deduplication layer.

### Layer 2: Jaccard Dedup Gate (`_find_near_duplicate`)

Even if the LLM returns a `create` action, the ingestion handler runs a Jaccard similarity check before writing:

1. Search the vault for up to 5 candidate facts using the new claim as a query.
2. For each candidate, compute word-level Jaccard similarity against the new claim.
3. If any candidate has Jaccard ≥ 0.70, treat it as a near-duplicate: **reinforce** the existing fact instead of creating a new one.

This catches cases where the same fact is phrased differently across conversations.

### Layer 3: Reweave (`_reweave_fact`)

After a new fact is written, the reweave pass looks for stale versions to supersede:

**Pass 1 — Explicit contradicts links:** Any fact IDs listed in the new fact's `contradicts` field are immediately superseded. The old fact's `state` is set to `superseded`, `superseded_by` points to the new fact, and `valid_until` is stamped with the current time.

**Pass 2 — Jaccard-based supersession:** Candidates of the same type with Jaccard overlap in the range **[0.40, 0.64]** (same topic, partially updated) are also superseded. The narrow range avoids both near-duplicates (>0.64, handled by Layer 2) and unrelated facts (<0.40). At least one substantive non-stopword must overlap to avoid false positives.

### Auto-Linking (`_auto_link_fact`)

After a new fact is written, related existing facts are found via search and added to `related_to`, building the knowledge graph automatically. Near-duplicates (Jaccard ≥ 0.6) are excluded from auto-linking — same-concept rephrases are not useful cross-links.

---

## Lifecycle Management

The vault runs periodic maintenance to keep facts healthy and the store from growing unbounded. Lifecycle is configured under `vault.lifecycle` in `pyclawops.yaml`.

### Crystallization

A `provisional` fact becomes `crystallized` when either:
- It has been reinforced at least `crystallize_reinforcements` times (default: 3), or
- It is at least `crystallize_days` days old (default: 7)

Crystallized facts are more trusted and rank higher in PLANNING and INCIDENT profiles.

### Forgetting

Facts with confidence below 0.3 and no reinforcements that have not been crystallized within `forget_days` (default: 30) are archived and removed from the active index.

### Tier Compression

Facts are progressively compressed as they age to reduce storage and context size:

| Tier | Description | Trigger |
|------|-------------|---------|
| 1 | Full: claim + body + all metadata | Default |
| 2 | Fact-only: claim + metadata, body stripped | After `tier1_to_2_days` (default: 30) |
| 3 | Summary: short claim only | After `tier2_to_3_days` (default: 90) |
| 4 | Tags only | After `tier3_to_4_days` (default: 365) |

### Hypothesis Promotion/Archival

Hypotheses (`type=hypothesis`) are reviewed during lifecycle runs:
- Promoted to `fact` if `confidence` rises above 0.75 (reinforced multiple times)
- Archived if `confidence` drops below 0.3 or no reinforcement after `forget_days`

### Anti-Memory Expiry

Facts of type `anti` can have `expires_at` set. The lifecycle reaper archives them when they expire.

---

## Retrieval Profiles

The `RetrievalProfile` determines how the assembled fact list is sorted before the `injection_limit` cap is applied. The profile is either auto-inferred from the query or set explicitly via `vault.default_profile` in config.

### Auto-Inference

The query text is matched against regex patterns (checked in priority order):

| Profile | Trigger keywords |
|---------|-----------------|
| `INCIDENT` | outage, incident, sev1–sev4, p0–p3, broken, failure, urgent, rollback, hotfix, degraded, error, exception, crash, bug |
| `PLANNING` | plan, planning, design, architect, roadmap, proposal, spec, migrate, migration, approach, strategy, implement |
| `HANDOFF` | resume, continue, handoff, pick up, where did I, last session, where we left, left off |
| `RESEARCH` | research, investigate, study, look into, what do we know, summarize, overview of |
| `DEFAULT` | Everything else |

### Profile Behaviors

| Profile | Sort key | Use case |
|---------|----------|----------|
| `DEFAULT` | `confidence × recency_weight` | Balanced — recent high-confidence facts first |
| `PLANNING` | `2× boost for decision/lesson types × confidence` | Architecture discussions |
| `INCIDENT` | `confidence` descending | Debugging — most reliable facts first |
| `HANDOFF` | `written_at` descending | Session resume — most recent facts first |
| `RESEARCH` | `2× boost for source_file facts × confidence` | Document-extracted knowledge first |

Recency weight decays by 50% every 30 days: `1.0 / (1.0 + age_days / 30.0)`.

---

## Wikilinks and the Knowledge Graph

Facts can reference each other using Obsidian-style wikilink syntax in their `body` field. The vault resolves these links at write time into typed graph edges.

### Link Types

```
[[target claim]]              → related_to (generic similarity)
[[depends_on::target claim]]  → depends_on (this fact requires target to be true)
[[part_of::target claim]]     → part_of (this is a component of a larger fact/project)
[[contradicts::target claim]] → contradicts (triggers auto-supersession of target)
```

Rules:
- Links only belong in `body`, never in `claim`
- Only use typed links when the relationship is unambiguous
- `[[contradicts::X]]` triggers an immediate supersession of X during the reweave pass

### Graph Expansion at Retrieval Time

When context is assembled, the result set is expanded via BFS through graph edges (`related_to`, `depends_on`, `part_of`). Each hop applies a `0.85` score multiplier, so deeper links naturally rank lower. This means asking about a project fact can surface related decisions and lessons even if they didn't score directly in the keyword search.

`contradicts` edges are explicitly **not** followed during expansion — contradicted facts are superseded/archived and filtered out anyway.

### Format for Injection

When facts with graph links are formatted for injection, typed relationships are surfaced inline:

```xml
<memory>
  <fact type="decision">Redis used for caching in AZDB project</fact>
  → depends on: [project] AZDB: distributed key-value store for embedded use
</memory>
```

---

## Session-Level Seen-Fact Cache

To avoid injecting the same memory repeatedly into the same conversation, each agent maintains a per-session in-memory cache of injected fact IDs.

**How it works:**
- `Agent._session_seen_facts: dict[str, set[str]]` — keyed by `session_id`, stores ULIDs of facts already injected this session.
- Before injection, `ctx.facts` is filtered to exclude already-seen IDs. Only genuinely new facts are injected.
- After injection, the new fact IDs are added to the session's seen set.
- The fetch limit is **not** inflated to compensate for seen facts — if all top results have been seen, nothing is injected. This is by design: repeat injection wastes tokens.

**Cache lifecycle:**
- Created on first injection for a session.
- Cleared when `evict_session_runner(session_id)` is called — which fires on `/reset`, `/new`, and on runner error.
- Never persisted to disk. On restart, the cache starts empty and facts are eligible for re-injection once. After the first pass they won't repeat within the new session.

---

## Cursor Store and Crash Recovery

The cursor store (`pyclawops/memory/vault/cursor.py`) tracks ingestion progress to ensure:
1. No conversation segment is processed twice.
2. No segment is silently skipped due to a crash.

State is persisted in `vault/.cursors.json` with this structure:

```json
{
  "currently_processing": null,
  "sessions": {
    "2026-03-10-abc123": {
      "session_id": "2026-03-10-abc123",
      "last_message_index": 42,
      "last_processed_at": "2026-03-10T22:15:00Z",
      "channel": "telegram"
    }
  },
  "documents": {
    "/Users/jon/.pyclawops/agents/niggy/memory/2026-03-10.md": {
      "file_path": "...",
      "last_hash": "sha256hex",
      "last_processed_at": "2026-03-10T22:15:00Z",
      "extracted_fact_ids": ["01KMGEGB...", "01KMGEGC..."]
    }
  }
}
```

**Crash recovery:** Before processing any segment, `currently_processing` is set to a description of the in-progress item. On startup, if `currently_processing` is non-null, it means the previous run crashed mid-ingestion. The stuck marker is cleared and that segment will be re-processed on the next catch-up pass.

**Document deletion:** When a memory document is deleted, `handle_document_deleted()` archives all facts that were extracted from it and clears the document cursor entry.

---

## Configuration

All vault configuration lives under the `vault:` key in `pyclawops.yaml` per agent.

```yaml
agents:
  - name: niggy
    vault:
      enabled: true
      path: ""                  # empty = default ~/.pyclawops/agents/niggy/vault/
      show_recall: false        # append injected facts to agent replies (debug)
      default_profile: auto     # auto | default | planning | incident | handoff | research

      agent:
        enabled: true
        model: ""               # empty = use main agent model
        max_tokens: 2048
        channels: [telegram, slack, tui, http]   # which channels trigger extraction
        min_turns: 2            # min messages before extraction runs

      lifecycle:
        crystallize_reinforcements: 3   # reinforce count to crystallize
        crystallize_days: 7             # days to crystallize regardless of reinforcements
        forget_days: 30                 # days before low-confidence facts are archived
        tier1_to_2_days: 30
        tier2_to_3_days: 90
        tier3_to_4_days: 365

      search:
        backend: fallback               # fallback | hybrid
        qmd_path: ""                    # path to qmd binary (hybrid only)
        qmd_collection: ""              # qmd collection name (hybrid only)
        injection_limit: 5              # max facts injected per query
        confidence_threshold: 0.5       # min confidence to include a fact
        min_relevance_score: 0.5        # min normalized search score (0–1)
        min_query_words: 3              # skip injection for very short queries
        graph_hops: 2                   # BFS depth for graph expansion

      types:
        - name: custom_type
          description: A custom memory type
          keywords: [keyword1, keyword2]
          color: "#6366f1"
```

### Key Thresholds

| Parameter | Default | Effect |
|-----------|---------|--------|
| `injection_limit` | 5 | Hard cap on facts injected per message |
| `confidence_threshold` | 0.5 | Filters low-confidence facts before scoring |
| `min_relevance_score` | 0.5 | Normalized 0–1; filters weak search matches |
| `min_query_words` | 3 | Short queries skip injection entirely |
| `graph_hops` | 2 | BFS depth — 0 disables graph expansion |

---

## Storage Layout

```
~/.pyclawops/agents/{agent_id}/vault/
├── .cursors.json           # Ingestion progress + crash recovery state
├── facts/
│   ├── 01KMGEGB....md     # Active VaultFact files (ULID-named)
│   └── ...
└── archive/
    ├── 01KMGFA0....md     # Superseded and archived facts
    └── ...
```

Facts are stored as Markdown files with YAML frontmatter. This means the vault is human-readable, diffable, and can be inspected or manually edited in any text editor. The ULID naming scheme ensures facts sort chronologically by creation time.

The vault is entirely self-contained in the agent's directory. Wiping and re-syncing is as simple as deleting `vault/facts/`, `vault/archive/`, and resetting `.cursors.json` to `{"currently_processing": null, "sessions": {}, "documents": {}}`.
