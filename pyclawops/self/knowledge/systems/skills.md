# Pyclaw Skill System — Design & Implementation Notes

Skills are modular, user-installable capability packages. A skill is a folder
containing a `SKILL.md` file (prompt template + metadata) plus optional scripts
and reference files. When invoked, the skill body is injected into the agent's
context for that turn — the agent then uses its MCP tools to execute.

**Core design principle:** `SKILL.md` is an interface, not an encyclopedia.
Keep it short. Put complexity in scripts. Scripts run via `uv run` or `bash`.
The agent never needs to read the script — just knows how to invoke it.

---

## Format: SKILL.md

Compatible with OpenClaw's format. Every skill is a folder with a `SKILL.md`:

```
~/.pyclawops/skills/
  my-skill/
    SKILL.md           ← required
    scripts/           ← optional: executable code
      do_thing.py      ← python (uv PEP 723 deps inline)
      helper.sh        ← bash
    references/        ← optional: docs loaded on demand
      api-schema.md
    assets/            ← optional: output templates, files
      template.html
```

### Frontmatter

```yaml
---
# REQUIRED
name: skill-name                    # lowercase, hyphens, max 64 chars
description: |                      # what it does AND when to trigger it
  Summarizes a URL or pasted text into bullet points.
  Triggers on: "summarize", "tldr", "give me bullet points"

# PYCLAW ADDITIONS
agent: assistant                    # which agent handles this (default: default agent)
channels: [telegram, tui, slack]    # where it's available (default: all)
inject: user                        # system | user — how body enters context (default: user)
tools: [memory, jobs]               # tool category hints for future proxy system
schedule: false                     # can this skill be scheduled as a job?

# DISPLAY / DISCOVERY
user-invocable: true                # show in /help (default: true)
emoji: "📝"

# COMPATIBILITY / REQUIREMENTS
requires:
  bins: [ffmpeg]                    # executables that must be in PATH
  env: [OPENAI_API_KEY]             # env vars that must be set
os: [darwin, linux]                 # platform restriction (default: all)
---
```

### Body (Markdown)

The body is what gets injected into the agent's context. **Keep it short.**
For simple skills: a few sentences of instructions.
For complex skills: just tell the agent which script to run and with what args.

```markdown
# Summarize

Summarize the provided content into clear bullet points.

If the user passed a URL, fetch it first with the fetch tool.
If the user passed text directly, summarize it as-is.

Format: 5-10 bullets, each starting with a bold keyword.
End with a one-sentence "Bottom line:".
```

Complex skill body (delegates to script):

```markdown
# Daily Briefing Setup

Set up a daily news briefing job for the user.

Run:
  uv run {baseDir}/scripts/setup_briefing.py --time "{args}"

The script will ask clarifying questions and create the appropriate job.
Report what was created when done.
```

---

## Template Variables

| Variable   | Value                                      |
|------------|--------------------------------------------|
| `{baseDir}`| Absolute path to this skill's folder       |
| `{args}`   | Raw argument string passed by user          |
| `{agent}`  | Name of the agent handling this turn        |
| `{channel}`| Channel this was invoked from (telegram...) |
| `{user}`   | User identifier for this channel            |
| `{session}`| Current session ID                          |

---

## Scripts: The Key Design Decision

The user's core insight: **the SKILL.md body should be short**. Move complexity
into scripts. The agent invokes the script and reports results. It never needs to
read or understand the implementation.

### Python scripts (preferred for anything non-trivial)

Use **PEP 723 inline metadata** so `uv run` handles all dependencies automatically.
No virtualenvs, no pip install, no requirements.txt:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "rich>=13.0",
# ]
# ///

import httpx
from rich.console import Console
# ... rest of script
```

Invoked from SKILL.md body:
```
uv run {baseDir}/scripts/do_thing.py --arg1 value --arg2 "{args}"
```

uv downloads and caches deps on first run, near-instant thereafter.

### Bash scripts (for shell operations, simple wrappers)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Always strict mode. Always.
```

### Decision tree: which script type?

```
Does it need external libraries?     → Python (uv + PEP 723)
Is it mostly shell commands?         → Bash
Is it pure Python stdlib?            → Python (no uv block needed, just python3)
Is it 3 lines of logic?              → Inline instructions in SKILL.md (no script)
Is it a complex multi-step workflow? → Python (uv)
Does it call APIs?                   → Python (uv, httpx/requests)
```

### Script conventions

- **Exit codes**: 0 = success, non-zero = failure (agent sees stderr)
- **Output**: Print results to stdout; agent reads and relays to user
- **Args**: Use `argparse` (Python) or `case` loops (bash)
- **Error messages**: Human-readable to stderr
- **No hardcoded paths**: Use args or env vars
- **Idempotent where possible**: Safe to re-run

---

## Discovery & Precedence

Pyclaw searches locations in order (later overrides earlier on name conflict):

```
1. pyclawops/skills/              bundled with package (lowest priority)
2. ~/.pyclawops/skills/           user-installed (managed)
3. <project>/skills/           workspace (highest priority)
```

Each location: top-level `SKILL.md` or subdirs each with `SKILL.md`.

**Limits (sensible defaults, configurable):**
- Max 200 skills loaded total
- Max 100 skills injected into system prompt
- Max 5KB of skill metadata per request (name + description only in prompt)
- Max 256KB per SKILL.md file

---

## Invocation

### Slash command (primary)

```
/skill-name
/skill-name some arguments here
```

- Command dispatcher checks built-in commands first
- Falls through to skill registry if no built-in matches
- Skill body + args injected into agent message
- Routed to skill's specified agent (or default)

### Natural trigger (secondary)

Skills with descriptive `description` fields can be triggered automatically
by the agent when the description matches the user's intent. This is the
OpenClaw model — the LLM reads skill metadata in the system prompt and
decides when to apply a skill without explicit invocation.

Pyclaw can support both: explicit `/invoke` and implicit trigger via system
prompt metadata.

---

## Execution Flow

```
User: /daily-briefing 9am
         │
         ▼
CommandRegistry.dispatch("/daily-briefing", "9am")
         │
         ├─ built-in command? → no
         ▼
SkillRegistry.lookup("daily-briefing")
         │
         ├─ load SKILL.md body
         ├─ replace {args} → "9am", {baseDir} → "~/.pyclawops/skills/daily-briefing"
         ▼
Construct agent message:
  [system: skill body injected here]
  [user: /daily-briefing 9am]
         │
         ▼
Agent (with full MCP tool access)
         │
         ├─ reads skill instructions
         ├─ runs: uv run ~/.pyclawops/skills/daily-briefing/scripts/setup.py --time "9am"
         ├─ reads script output
         ▼
Response to user
```

---

## Bundled Skills (Ship with Pyclaw)

### `skill-creator` — The Meta-Skill (Most Important)

Guides creating new skills. When user says "create a skill", "build me a skill
that does X", "I want a command that...":

1. Understand what the user wants (concrete examples)
2. Decide: inline instructions, bash script, or Python+uv?
3. Run `uv run {baseDir}/scripts/init_skill.py <name> --path ~/.pyclawops/skills`
4. Write the script(s) if needed (tested, with PEP 723 deps)
5. Write the SKILL.md body (short, references scripts)
6. Test by invoking the new skill
7. Report what was created and how to invoke it

The skill-creator itself uses a Python script for initialization and validation.
The agent writes the files, tests them, and the new skill is immediately available.

### Other bundled skills (initial set)

| Skill | Purpose | Script type |
|-------|---------|-------------|
| `skill-creator` | Build new skills | Python (uv) |
| `summarize` | Summarize URL or text | Inline |
| `job-setup` | Interactive job/cron creator | Python (uv) |
| `config-edit` | Guide config.yaml changes | Inline |
| `memory-browse` | Browse/search memory | Python (uv) |
| `agent-info` | Show agent status and config | Inline |
| `export-chat` | Export session to markdown | Python (uv) |

---

## Packaging & Distribution

### .skill files

Same format as OpenClaw — zip archive with `.skill` extension:

```
my-skill.skill (zip)
└── my-skill/
    ├── SKILL.md
    ├── scripts/
    │   └── script.py
    └── references/
        └── api.md
```

### Install from .skill file

```
/install-skill path/to/my-skill.skill
```

Or the agent can do it: "install this skill for me" + attach .skill file.

### Skill packages vs OpenClaw compatibility

Since the SKILL.md format is compatible, most OpenClaw skills that:
- Don't use `{baseDir}/bin/` (no compiled binaries)
- Don't require OpenClaw-specific env vars
- Use Python or bash scripts

...can be installed directly in Pyclaw. The `metadata.openclaw` block is just
ignored. This is a meaningful compatibility story.

---

## Validation

A `validate_skill.py` script (bundled in `skill-creator`) checks:

- `SKILL.md` exists
- Valid YAML frontmatter with `---` delimiters
- `name` and `description` are present
- Name format: `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (no leading/trailing hyphens)
- Name length: max 64 chars
- Description length: max 1024 chars
- No disallowed keys in frontmatter
- If `requires.bins` set: check binaries exist (optional, warn only)
- If scripts referenced: check files exist at `{baseDir}/scripts/...`

---

## Implementation Plan

### Phase 1: Core infrastructure

1. **`SkillRegistry`** class (`pyclawops/skills/registry.py`)
   - Discover skills from 3 locations
   - Parse frontmatter (PyYAML)
   - Build name → SkillEntry map
   - Filter by channel, os, user-invocable

2. **Skill dispatch** in `CommandRegistry`
   - After built-in lookup fails, try SkillRegistry
   - Return `None` (fall-through to agent) with skill body pre-injected

3. **Template variable substitution**
   - `{baseDir}`, `{args}`, `{agent}`, `{channel}`, `{user}`, `{session}`

4. **System prompt injection**
   - Skill metadata (name + description) injected as agent context
   - Enables implicit triggering

### Phase 2: Skill creator

5. **Bundled `skill-creator` skill** with:
   - `scripts/init_skill.py` — scaffold generator
   - `scripts/validate_skill.py` — validator

6. **`/skills` command** — list available skills

7. **`/install-skill <path>` command** — install from .skill file

### Phase 3: Polish

8. **Skill reload on file change** (dev mode)
9. **Per-channel skill filtering**
10. **Skill usage logging** (which skills are used, agent feedback loop)
11. **`/make-skill` shortcut** — quick skill creation flow

---

## Config Schema Addition

```yaml
# pyclawops.yaml
skills:
  enabled: true
  dirs:
    - ~/.pyclawops/skills         # managed
    # - /custom/path           # extra dirs
  allow_bundled: []            # empty = all bundled skills allowed
  max_in_prompt: 100           # max skills injected into system prompt
  auto_trigger: true           # allow implicit triggering from description
```

---

## Key Differences from OpenClaw

| Aspect | OpenClaw | Pyclaw |
|--------|----------|--------|
| Runtime | Coding agent (local) | Messaging bot (any channel) |
| Channels | Chat window | Telegram, TUI, Slack, WhatsApp |
| Agent routing | Single agent | `agent:` field routes to named agent |
| Tool system | OpenClaw tools | MCP tools via FastAgent |
| Scheduling | N/A | Skills can create/be jobs |
| Script runner | `python3` / `bash` | `uv run` preferred / `bash` |
| Distribution | .skill zip | .skill zip (same format) |
| Self-improvement | skill-creator skill | skill-creator + can write to memory |
| Memory | N/A | Skills can read/write memory via MCP tools |

---

## Open Questions

1. **Implicit triggering**: Do we want the LLM to auto-trigger skills based on
   description, or only explicit `/skill-name` invocation? OpenClaw does both.
   Auto-triggering burns context (all skill metadata in prompt) but is more
   magical. Suggest: opt-in per skill via `auto-trigger: true` in frontmatter.

2. **Skill arguments**: Raw string pass-through (OpenClaw style) or structured
   argparse-style parsing at the framework level? Raw is simpler; structured
   enables better error messages. Suggest: raw passthrough, let script handle it.

3. **Agent-scoped skills**: Should skills be definable per-agent in `pyclawops.yaml`
   (agent-specific skill dirs) or always global? Suggest: global discovery with
   `agent:` field in frontmatter for routing.

4. **Skill versioning**: Should `.skill` files include a version field? Useful
   for update checking. Add `version: "1.0.0"` to frontmatter optionally.

5. **Cross-skill invocation**: Can a skill call another skill? OpenClaw doesn't
   support this explicitly but the agent could invoke `/other-skill` in a message.
   Probably fine to leave implicit for now.

6. **Memory integration**: Should skills have read/write access to memory tools
   by default? Enabling skill-level memory (e.g., skill remembers user preferences
   across invocations). This is powerful but needs security consideration.

---

*Research session: 2026-03-09*
*Based on: OpenClaw skills/ analysis + Pyclaw architecture review*
*Related: docs/TOOL_RESEARCH.md*
