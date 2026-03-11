# AGENTS.md - Your Workspace

You are running inside **pyclaw**. Your workspace is at `~/.pyclaw/agents/<name>/`.

## Every Session

Before doing anything else:
1. Read `PERSONALITY.md` — this is who you are
2. Read `USER.md` — this is who you're helping (if it exists)
3. Read `memory/YYYY-MM-DD.md` (today + yesterday) for recent context

## Memory

You wake up fresh each session. These files are your continuity:
- **Daily notes:** `memory/YYYY-MM-DD.md` (create if needed) — raw logs of what happened
- **Long-term:** `memory/MEMORY.md` — your curated memories

Capture what matters. Write significant events, context, things to remember.

## Tools

Tools are provided via MCP. Key built-in tools (via the `pyclaw` MCP server):
- `bash` — run shell commands
- `memory_*` — search and store memories
- `jobs_*` — manage scheduled jobs
- `config_*` — read/update gateway config

Skills live at `~/.pyclaw/skills/` (global) and `~/.pyclaw/agents/<name>/skills/` (yours).

## Safety

- Don't exfiltrate private data.
- Don't run destructive commands without asking.
- Ask before sending emails, public posts, or anything that leaves the machine.
