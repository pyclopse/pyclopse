# Migrating from OpenClaw to pyclopse

This guide covers migrating an agent from the OpenClaw TypeScript gateway to pyclopse. It is based on the real migration of the `viavacavi` agent and applies to any agent.

---

## Overview

OpenClaw and pyclopse use similar concepts but different layouts and naming conventions.

| Concept | OpenClaw path | pyclopse path |
|---|---|---|
| Agent workspace | `~/.openclaw/agents/{name}/workspace/` | `~/.pyclopse/agents/{name}/` |
| Sessions / chat history | `~/.openclaw/agents/{name}/sessions/*.jsonl` | `~/.pyclopse/agents/{name}/sessions/{date}-{id}/` |
| Global config | `~/.openclaw/openclaw.json` | `~/.pyclopse/config/pyclopse.yaml` |

### File name mapping

| OpenClaw file | pyclopse file | Notes |
|---|---|---|
| `SOUL.md` | `PERSONALITY.md` | Rename on copy |
| `HEARTBEAT.md` | `PULSE.md` | Rename on copy; update script paths |
| `IDENTITY.md` | `IDENTITY.md` | Copy as-is |
| `USER.md` | `USER.md` | Copy as-is |
| `AGENTS.md` | `AGENTS.md` | **Do not copy** — replace with pyclopse template (see below) |
| `TOOLS.md` | `TOOLS.md` | Copy as-is |
| `BOOTSTRAP.md` | *(skip)* | One-time init file; already consumed |
| `memory/` | `memory/` | Copy entire directory |
| `docs/` | `docs/` | Copy entire directory |
| `scripts/` | `scripts/` | Copy entire directory; update paths inside |
| `skills/` | `~/.pyclopse/agents/{name}/skills/` | Copy skills you want to keep |

---

## Step 1 — Add the agent to pyclopse config

Edit `~/.pyclopse/config/pyclopse.yaml` and add the agent under `agents:`.

```yaml
agents:
  myagent:
    name: MyAgent
    model: generic.MiniMax-M2.5      # or claude-sonnet-4-5, etc.
    use_fastagent: true
    request_params:
      reasoning_split: true           # only for models with thinking
    heartbeat:
      enabled: true
      every: 15m
      activeHours:
        start: "05:00"
        end: "22:00"
    mcp_servers: [pyclopse, fetch, time, filesystem]
    tools:
      profile: full
```

---

## Step 2 — Add the Telegram bot (multi-bot mode)

Each agent gets its own Telegram bot token. Add it to the `channels.telegram.bots` section:

```yaml
channels:
  telegram:
    allowedUsers: [YOUR_TELEGRAM_USER_ID]
    enabled: true
    streaming: true
    bots:
      main:
        botToken: "${env:MAIN_BOT_TOKEN}"   # or literal token
        agent: main
      myagent:
        botToken: "${env:MYAGENT_BOT_TOKEN}"
        agent: myagent
```

The bot token comes from `~/.openclaw/openclaw.json` — look for the agent's `telegramBot.token` field.

Per-bot overrides (allowedUsers, deniedUsers, streaming, typingIndicator) are optional; they inherit from the parent `telegram:` block if omitted.

---

## Step 3 — Copy workspace files

Create the agent directory and copy files, renaming as needed:

```bash
mkdir -p ~/.pyclopse/agents/myagent

# Core identity files
cp ~/.openclaw/agents/myagent/workspace/IDENTITY.md ~/.pyclopse/agents/myagent/IDENTITY.md
cp ~/.openclaw/agents/myagent/workspace/SOUL.md     ~/.pyclopse/agents/myagent/PERSONALITY.md
cp ~/.openclaw/agents/myagent/workspace/USER.md     ~/.pyclopse/agents/myagent/USER.md
cp ~/.openclaw/agents/myagent/workspace/TOOLS.md    ~/.pyclopse/agents/myagent/TOOLS.md

# Heartbeat → Pulse (rename, then update paths — see Step 4)
cp ~/.openclaw/agents/myagent/workspace/HEARTBEAT.md ~/.pyclopse/agents/myagent/PULSE.md

# Memory and supporting directories
cp -r ~/.openclaw/agents/myagent/workspace/memory  ~/.pyclopse/agents/myagent/memory
cp -r ~/.openclaw/agents/myagent/workspace/scripts ~/.pyclopse/agents/myagent/scripts
cp -r ~/.openclaw/agents/myagent/workspace/docs    ~/.pyclopse/agents/myagent/docs   # if present

# Skills (optional — skip any you don't want)
cp -r ~/.openclaw/agents/myagent/workspace/skills/myskill ~/.pyclopse/agents/myagent/skills/myskill
```

**Do not copy** `AGENTS.md`, `BOOTSTRAP.md`, or `sessions/` from the workspace.

---

## Step 4 — Replace AGENTS.md with the pyclopse template

The OpenClaw `AGENTS.md` tells the agent it is running inside OpenClaw with OpenClaw paths. Replace it entirely:

```bash
cp /path/to/pyclopse/pyclopse/core/templates/AGENTS.md ~/.pyclopse/agents/myagent/AGENTS.md
```

If you installed pyclopse as a `uv tool`, find the template at:

```bash
$(uv tool dir)/pyclopse/lib/python*/site-packages/pyclopse/core/templates/AGENTS.md
```

The template says "You are running inside pyclopse" and references the correct `~/.pyclopse/agents/<name>/` paths.

---

## Step 5 — Update script paths in PULSE.md

PULSE.md (formerly HEARTBEAT.md) will still contain OpenClaw paths like:

```
/Users/you/.openclaw/workspace-myagent/scripts/my-script.sh
```

Update these to the new pyclopse location:

```
~/.pyclopse/agents/myagent/scripts/my-script.sh
```

Also update any SP-API skill paths that referenced `~/.openclaw/workspace/skills/`.

---

## Step 6 — Extract RULES.md (optional but recommended)

OpenClaw's `SOUL.md` often contains a "Boundaries" or "Rules" section. pyclopse injects a dedicated `RULES.md` with special emphasis in the system prompt:

> **IMPORTANT: The following rules were set by the user. They are mandatory and must be followed at all times.**

Extract those rules from `PERSONALITY.md` into a separate `RULES.md`:

```bash
# Create ~/.pyclopse/agents/myagent/RULES.md with the extracted content
```

---

## Step 7 — Import chat history

pyclopse includes a built-in importer that converts OpenClaw JSONL sessions into pyclopse's FastAgent-native format:

```bash
# Import one agent
pyclopse import-openclaw --agent myagent

# Import all agents at once
pyclopse import-openclaw --all

# Custom directories
pyclopse import-openclaw --agent myagent \
  --openclaw-dir ~/backup/openclaw \
  --pyclopse-dir ~/.pyclopse
```

The importer:
1. Reads each `.jsonl` file from `~/.openclaw/agents/{name}/sessions/`
2. Extracts `user` and `assistant` messages (skipping tool calls, thinking blocks, metadata)
3. Converts to FastAgent's `PromptMessageExtended` JSON format
4. Writes to `~/.pyclopse/agents/{name}/sessions/{YYYY-MM-DD}-{6chars}/history.json`
5. Writes `session.json` with `channel: "openclaw"` and `metadata.imported_from: "openclaw"`

Imported sessions are loaded into the TUI session list on the next gateway start and are fully resumable.

After importing, the raw JSONL files in `~/.pyclopse/agents/{name}/sessions/` (if you copied them manually before running the importer) can be deleted — they are not used by pyclopse.

---

## Step 8 — Verify the directory layout

After migration your agent directory should look like this:

```
~/.pyclopse/agents/myagent/
├── AGENTS.md          ← pyclopse template (not OpenClaw's)
├── IDENTITY.md
├── PERSONALITY.md     ← was SOUL.md
├── PULSE.md           ← was HEARTBEAT.md, paths updated
├── RULES.md           ← extracted from SOUL.md (optional)
├── TOOLS.md
├── USER.md
├── docs/
├── memory/
│   ├── 2026-02-21.md
│   └── heartbeat-state.json
├── scripts/
│   └── my-script.sh
├── sessions/
│   ├── 2026-02-21-aB3xYz/
│   │   ├── session.json
│   │   └── history.json
│   └── ...
└── skills/            ← if any
    └── myskill/
        └── SKILL.md
```

---

## Step 9 — Start the gateway and test

```bash
uv run python -m pyclopse run
```

Startup output should show both bots initialised:

```
Telegram polling: ['main', 'myagent']
```

Send a message to the agent's Telegram bot to verify inbound routing. The agent's heartbeat will fire on its configured schedule and deliver to the correct bot.

---

## What pyclopse auto-injects

You do **not** need to manually include these in your agent files — pyclopse handles them:

| Feature | How |
|---|---|
| `AGENTS.md` through `PULSE.md` | Injected into system prompt via `prompt_builder.py` |
| Skills | Injected as `<available_skills>` XML at end of system prompt |
| `MEMORY.md` | Injected via `boot-md` hook at startup (if present) |
| Conversation history | Loaded from `history.json` into FastAgent on first turn |

---

## Key differences from OpenClaw

| Topic | OpenClaw | pyclopse |
|---|---|---|
| Language | TypeScript / Node.js | Python / asyncio |
| Config format | `openclaw.json` (JSON) | `pyclopse.yaml` (YAML) |
| Session format | JSONL event log | FastAgent `PromptMessageExtended` JSON |
| Session location | `~/.openclaw/agents/{name}/sessions/` | `~/.pyclopse/agents/{name}/sessions/{date}-{id}/` |
| Workspace location | `~/.openclaw/agents/{name}/workspace/` | `~/.pyclopse/agents/{name}/` (flat, no `workspace/` subdirectory) |
| Personality file | `SOUL.md` | `PERSONALITY.md` |
| Heartbeat file | `HEARTBEAT.md` | `PULSE.md` |
| Rules | Embedded in `SOUL.md` | Separate `RULES.md` (pyclopse addition) |
| Multi-bot Telegram | Per-agent `telegramBot` in config | `channels.telegram.bots` dict |
| MCP tools | OpenClaw built-ins | pyclopse FastMCP server on port 8081 |
