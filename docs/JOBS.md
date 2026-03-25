# pyclawops Jobs System

The Jobs system lets you schedule automated work — shell commands or agent prompts — on cron, interval, or one-shot schedules. Results are delivered to a messaging channel, posted to a webhook, or silently discarded.

---

## Overview

```
JobScheduler._loop()
    → _tick() — checks next_run for each enabled job every 10s
        → _run_job(job) — executes in a background asyncio task
            → _run_command(job)  — shell command via subprocess
            → _run_agent(job)    — calls gateway._agent_executor(job)
                → build_job_prompt()       — constructs system prompt from AgentRun flags
                → _get_or_create_session() — isolated (unique) or persistent (shared)
                                             NOTE: job sessions never become the agent's
                                             active_session; they bypass that system entirely
                → agent.handle_message()   — runs through FastAgent
                → evict_session_runner()   — cleanup for isolated sessions
                → _deliver_to_session()    — if report_to_agent is set, delivers result
                                             to the named agent's current active channel
```

Jobs are persisted to `~/.pyclawops/agents/{agent_id}/jobs.yaml` (one file per agent) and survive restarts. Each run is appended to a per-job JSONL log under `~/.pyclawops/agents/{agent_id}/runs/`.

---

## Job Model

```python
Job(
    id:               str           # UUID, auto-generated
    name:             str           # human-readable label
    description:      str | None
    enabled:          bool          # False = skip all scheduling
    tags:             list[str]

    run:              CommandRun | AgentRun     # what to execute
    schedule:         CronSchedule | IntervalSchedule | AtSchedule  # when
    deliver:          DeliverNone | DeliverAnnounce | DeliverWebhook  # where results go

    on_failure:       FailureAlert | None       # alert after N consecutive errors
    timeout_seconds:  int           # default 300 — hard kill after this
    max_retries:      int           # default 0
    delete_after_run: bool          # one-shot: delete on success (AtSchedule only)

    # Runtime state (managed by scheduler)
    status:           pending | running | completed | failed | disabled
    next_run:         datetime | None
    last_run:         datetime | None
    run_count:        int
    failure_count:    int
    consecutive_errors: int
)
```

---

## Run Types

### CommandRun — shell command

```json
"run": {
  "kind": "command",
  "command": "cd /some/dir && uv run python script.py"
}
```

Executes via `asyncio.create_subprocess_shell`. Stdout/stderr captured and delivered.

### AgentRun — agent prompt

```json
"run": {
  "kind": "agent",
  "agent": "ritchie",
  "message": "Run the trading-scan skill.",
  "model": null,

  "session_mode": "isolated",
  "prompt_preset": "full",

  "include_personality": true,
  "include_identity":    true,
  "include_rules":       true,
  "include_memory":      true,
  "include_user":        true,
  "include_agents":      true,
  "include_tools":       true,
  "include_skills":      true,

  "instruction": null,
  "report_to_agent": null
}
```

The agent receives `message` as the user turn. The system prompt is assembled at runtime from the `include_*` flags (see [Prompt Control](#prompt-control) below).

**`report_to_agent`** — if set to another agent's ID, the job result is delivered into that agent's current active session and channel after completion. This lets one agent's job output surface in another agent's conversation thread without manual routing. Example: `"report_to_agent": "niggy"` — Ritchie's TradingScan result gets posted into whatever channel Niggy is currently active on.

Job sessions themselves never become the running agent's `active_session` — they are always ephemeral and bypass the active-session pointer system. See [sessions.md](sessions.md) for the full session model.

#### Delivery tokens (report_to_agent only)

When `report_to_agent` is set, the isolated agent's response is inspected for a delivery token on the **first line**. The token controls how — and whether — the result is surfaced to the user.

| Token | Behaviour |
|---|---|
| `NO_REPLY …` (≤ 100 chars total) | Suppresses all delivery. The raw result is still injected into the target agent's session history as a synthetic tool call, so the agent retains context. Nothing is sent to the user. |
| `SUMMARIZE …` | Passes the content (everything after `SUMMARIZE`) to the target agent's LLM with a "please summarize and report to the user" instruction. The resulting summary is delivered to the user's channel. |
| *(no token)* | Verbatim delivery. The full response is injected into history as a synthetic tool call + assistant turn, then delivered directly to the user without an extra LLM call. |

Tokens are case-insensitive (`no_reply`, `NO_REPLY`, and `No_Reply` all work).

**`NO_REPLY` rules:** The entire response — including the `NO_REPLY` prefix and any trailing text — must fit in ≤ 100 characters. If the response exceeds 100 chars, it falls through to verbatim delivery.

**History injection:** In all three cases (including `NO_REPLY`), the result is injected into the target agent's active session runner as a synthetic `scheduled_job` tool call + tool result pair. This means the agent always has the job output in its conversation context for the next user message, even when the user was never notified.

**Announce is unaffected:** `deliver: mode: announce` sends job start/complete status pings to the channel regardless of the token. Tokens only control what happens with `report_to_agent` result delivery — they have no effect on announce messages.

##### Instructing isolated agents to use tokens

Add a final step to the skill or job `instruction` that tells the isolated agent how to format its response. The response must be **only the token line** — no analysis, markdown, or other text before it. All reasoning belongs in the skill's context logging, not the response:

```
## Step 5: Return Your Result

Your entire response must be a single line — the token line only. No analysis, no
markdown, no other text before or after it.

- Nothing notable happened (HOLD / NO_TRADES): NO_REPLY <≤100 char note>
- Trade placed / closed / replaced / account alert: SUMMARIZE <plain-text details>
```

---

## Schedule Types

### CronSchedule

```json
"schedule": {
  "kind": "cron",
  "expr": "0 9 * * 1-5",
  "timezone": "America/New_York",
  "stagger_seconds": 0
}
```

Standard 5-field cron expression. `timezone` defaults to the scheduler's default (system local or `jobs.default_timezone` in config). `stagger_seconds` adds up to N seconds of random jitter — useful for spreading load when many jobs share the same cron time.

### CronSchedule — continuous mode

Replace the minutes field with the `continuous` keyword to make a job restart immediately after each run completes, for as long as the window is open:

```json
"schedule": {
  "kind": "cron",
  "expr": "continuous 7-14 * * 1-5",
  "timezone": "America/New_York"
}
```

Behaviour:
- When a run completes and `now()` is still within the window (`7-14 * * 1-5`), `next_run` is set to `now()` — the job fires again immediately (or after `stagger_seconds` if set).
- When `now()` is outside the window, `next_run` is set to the next window open time (e.g. next weekday at 07:00).
- On failure the job still restarts (within the window) — use `on_failure` alerting to catch persistent errors.

A run that starts before the window closes is allowed to finish past the boundary; it will not be hard-killed at the window edge.

### IntervalSchedule

```json
"schedule": {
  "kind": "interval",
  "seconds": 3600
}
```

Runs every N seconds. The interval starts from when the previous run *ended*, not when it *started*, so a slow job won't pile up concurrent runs.

### AtSchedule

```json
"schedule": {
  "kind": "at",
  "at": "2026-03-15T09:30:00"
}
```

One-shot: runs once at an absolute UTC datetime. Set `delete_after_run: true` to have the job remove itself after successful completion.

---

## Delivery Types

### DeliverAnnounce (default)

```json
"deliver": {
  "mode": "announce",
  "channel": "telegram",
  "chat_id": "12345678"
}
```

Sends the agent's response (or command stdout) to a messaging channel. If `channel` and `chat_id` are omitted, falls back to the agent's configured default channel/chat.

### DeliverNone

```json
"deliver": { "mode": "none" }
```

Run silently — no output delivered anywhere. Useful for side-effect jobs (writing files, updating state).

### DeliverWebhook

```json
"deliver": {
  "mode": "webhook",
  "url": "https://example.com/webhook"
}
```

HTTP POSTs the result JSON to a URL.

---

## Prompt Control (AgentRun)

Each `AgentRun` controls exactly what goes into the agent's system prompt for that job run. This is independent of the agent's normal conversation system prompt.

### session_mode

| Value | Behaviour |
|---|---|
| `isolated` (default) | Fresh session per run — no history from previous runs. Runner evicted after completion. |
| `persistent` | Shared session per job — history accumulates across runs. |

`isolated` is the default and correct choice for almost all jobs. Use `persistent` only when you explicitly want the agent to remember output from previous runs of the same job.

### prompt_preset

Presets set default values for all `include_*` flags. Individual flags override the preset.

| Preset | personality | identity | rules | memory | user | agents | tools | skills |
|---|---|---|---|---|---|---|---|---|
| `full` (default) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `minimal` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ |
| `task` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

### include_* flags

Each flag maps to a bootstrap file in `~/.pyclawops/agents/{agent}/`:

| Flag | File(s) loaded |
|---|---|
| `include_personality` | `PERSONALITY.md`, `SOUL.md` |
| `include_identity` | `IDENTITY.md` |
| `include_rules` | `RULES.md` |
| `include_memory` | `MEMORY.md`, `memory.md` |
| `include_user` | `USER.md` |
| `include_agents` | `AGENTS.md` |
| `include_tools` | `TOOLS.md` |
| `include_skills` | `<available_skills>` block (discovered from skills dirs) |

MCP tool connections (servers) are always active regardless of these flags — they're controlled by the agent's config, not the prompt.

### instruction

An optional string appended to the end of the assembled system prompt, after all `include_*` content. Works with any preset. If all flags are off and `instruction` is the only content, it becomes the entire system prompt.

### Common patterns

```json
// Full agent, fresh session — normal job for a fully-configured agent
{ "session_mode": "isolated", "prompt_preset": "full" }

// Minimal — persona + rules + skills, no memory overhead
{ "session_mode": "isolated", "prompt_preset": "minimal" }

// Task loop — headless executor with tools and a focused directive
{
  "session_mode": "isolated",
  "prompt_preset": "task",
  "include_tools": true,
  "include_skills": true,
  "instruction": "Follow all instructions given. No commentary, just act."
}

// Full agent but strip memory for this particular job
{ "session_mode": "isolated", "prompt_preset": "full", "include_memory": false }

// Persistent research job — accumulates findings across runs
{ "session_mode": "persistent", "prompt_preset": "full" }
```

---

## Failure Alerting

```json
"on_failure": {
  "alert_after": 3,
  "channel": "telegram",
  "chat_id": "12345678"
}
```

Sends an alert after `alert_after` consecutive failures. The counter resets on any successful run. If `channel`/`chat_id` are omitted, uses the deliver config.

---

## Persistence

- **Job definitions**: `~/.pyclawops/agents/{agent_id}/jobs.yaml` — per-agent YAML, keyed by job name, atomic write
- **Run logs**: `~/.pyclawops/agents/{agent_id}/runs/{job_id}.jsonl` — one JSON line per completed run

The scheduler merges on every flush: in-memory state wins for known jobs; jobs added externally while the scheduler is running are preserved rather than dropped.

---

## REST API

All endpoints are under `/api/v1/jobs`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | List all jobs |
| `GET` | `/status` | Scheduler status (total, enabled, running) |
| `GET` | `/{name_or_id}` | Get one job by ID or name |
| `POST` | `/command` | Create a command job |
| `POST` | `/agent` | Create an agent job |
| `PATCH` | `/{name_or_id}` | Update job fields (including prompt/session config) |
| `DELETE` | `/{name_or_id}` | Delete a job |
| `POST` | `/{name_or_id}/enable` | Enable a disabled job |
| `POST` | `/{name_or_id}/disable` | Disable without deleting |
| `POST` | `/{name_or_id}/run` | Trigger immediately |
| `GET` | `/{name_or_id}/history` | Run history (last 20) |

### PATCH fields

```json
{
  "name": "string",
  "description": "string",
  "enabled": true,
  "schedule": "0 9 * * 1-5 America/New_York",
  "timeout_seconds": 600,
  "deliver_channel": "telegram",
  "deliver_chat_id": "12345678",
  "deliver_webhook_url": "https://...",

  "session_mode": "isolated",
  "prompt_preset": "full",
  "include_personality": true,
  "include_identity": true,
  "include_rules": true,
  "include_memory": false,
  "include_user": true,
  "include_agents": true,
  "include_tools": true,
  "include_skills": true,
  "instruction": "Custom job directive appended to system prompt."
}
```

### Schedule string format (for POST/PATCH)

| Format | Example |
|---|---|
| bare cron (5 fields) | `0 9 * * 1-5` |
| cron with timezone (6th token) | `0 9 * * 1-5 America/New_York` |
| continuous cron | `continuous 7-14 * * 1-5` |
| interval shorthand | `30m`, `1h`, `2d` |
| ISO datetime (one-shot) | `2026-03-15T09:30:00` |

---

## Agent Access to Jobs

Agents interact with jobs through the **REST API** (via HTTP tools or the `bash` MCP tool) — there are no `job_*` MCP tools in the pyclawops MCP server. Agents can use:

- The `/job` slash command from any chat session (see below)
- `bash` MCP tool to call the REST API directly: `curl http://localhost:8080/api/v1/jobs`
- The `subagent_*` MCP tools for managing subagent-type jobs: `subagent_spawn`, `subagent_status`, `subagents_list`, `subagent_kill`, `subagent_interrupt`, `subagent_send`

---

## Slash Commands

From any chat session:

```
/job list                          — list all jobs and their status
/job add <agent> "<message>" every <N>s|m|h  — create interval agent job
/job add <agent> "<message>" cron "<expr>"   — create cron agent job
/job enable <name>                 — enable a job
/job disable <name>                — disable a job
/job run <name>                    — trigger immediately
/job delete <name>                 — delete permanently
/job help                          — show all subcommands
```

---

## Config

```yaml
jobs:
  enabled: true
  agentsDir: ~/.pyclawops/agents       # where per-agent jobs.yaml files live
  defaultTimezone: America/New_York  # used when a cron job has no explicit tz
```

`defaultTimezone` should be set to your local timezone to avoid surprises with cron expressions. Without it, pyclawops falls back to the system local timezone.

---

## Timezone Notes

Cron expressions are evaluated in the timezone specified on the `CronSchedule`. **Always set an explicit timezone on trading or time-sensitive jobs** — UTC is the default if neither the job nor `defaultTimezone` specifies one, which will be offset from wall-clock market hours.

Example: market hours are 9:30am–4:00pm ET (UTC-4 in summer, UTC-5 in winter).

```json
// Correct: runs every 5 min during market hours, ET-aware
"schedule": {
  "kind": "cron",
  "expr": "*/5 9-15 * * 1-5",
  "timezone": "America/New_York"
}

// Wrong: UTC cron — shifts by 4-5 hours relative to market open
"schedule": {
  "kind": "cron",
  "expr": "*/5 9-15 * * 1-5",
  "timezone": "UTC"
}
```

---

## Subagent System

The subagent system is a thin wrapper around the jobs system. When an agent calls `subagent_spawn`, pyclawops creates an ephemeral `AgentRun` job with:

- `persistent: false` — ephemeral job, never written to `jobs.yaml`
- `schedule: AtSchedule(at: now())` — fires immediately
- `delete_after_run: true` — removes itself from memory on completion
- `deliver: DeliverNone` — no default announce; results go via `report_to_session`

The scheduler's `spawn_subagent()` method handles creation and fires `run_job_now()` immediately. The job is never written to the agent's `jobs.yaml` — it lives only in memory for its short lifetime.

### Result Delivery

Subagent results are delivered back to the session that spawned them via `report_to_session` (a session ID captured at spawn time). This is more precise than `report_to_agent`, which targets the agent's *currently* active session — if the user switches sessions between spawn and completion, the result still goes to the right place.

### Subagent Jobs vs Regular Jobs

| Property | Regular Job | Subagent Job |
|---|---|---|
| Persisted to `jobs.yaml` | ✅ | ❌ (memory only) |
| Visible in `jobs_list()` | ✅ | ❌ (filtered out) |
| Shown in `subagents_list()` | ❌ | ✅ |
| Schedule | cron / interval / at | always `at: now()` |
| Delivery | announce / webhook / none | `report_to_session` only |
| Cancellable mid-run | ❌ | ✅ `subagent_kill` |

### MCP Tools

| Tool | Description |
|---|---|
| `subagent_spawn(task, agent?, model?, timeout?, prompt_preset?, instruction?)` | Spawn a background subagent; returns job_id immediately |
| `subagents_list()` | List active subagents for the calling agent |
| `subagent_status(job_id)` | Get details on one subagent |
| `subagent_kill(job_id)` | Cancel a running subagent |
| `subagent_interrupt(job_id, task)` | Kill and respawn with a new task (steer) |
| `subagent_send(job_id, message)` | Queue a follow-up turn for a running subagent |

### Slash Commands

```
/subagents list                        — list active subagents
/subagents status <id>                 — show subagent details
/subagents kill <id>                   — cancel a running subagent
/subagents interrupt <id> <new task>   — steer with a new task
/subagents send <id> <message>         — queue a follow-up message
```

### REST API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/subagents/` | Spawn a subagent |
| `GET` | `/api/v1/subagents/` | List subagents (optional `?agent=`) |
| `GET` | `/api/v1/subagents/{job_id}` | Get one subagent |
| `DELETE` | `/api/v1/subagents/{job_id}` | Kill a subagent |
| `POST` | `/api/v1/subagents/{job_id}/interrupt` | Interrupt and respawn |
| `POST` | `/api/v1/subagents/{job_id}/send` | Queue a follow-up message |
