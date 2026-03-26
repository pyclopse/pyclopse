# OpenClaw → PyClawOps Gap Analysis

> Source-verified. OpenClaw source at `~/github/openclaw`. FastAgent package at
> `.venv/lib/python3.14/site-packages/fast_agent/`. PyClawOps commands confirmed
> from `pyclawops/core/commands.py` and `pyclawops/config/schema.py`.
> PyClawOps is NOT a port — items are adapted to Python idioms.

## Legend
- ✅ Done
- ⬜ Not Started
- ❌ Won't Implement / Not Applicable

---

## Message Queue Modes

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | `followup` mode | |
| ✅ | `collect` mode (default) | |
| ✅ | `interrupt` mode | |
| ✅ | `steer` mode | |
| ✅ | `steer-backlog` mode | |
| ✅ | `debounce_ms`, `cap`, `drop` knobs | |
| ✅ | `/queue` slash command | |
| ✅ | `queue` mode | OC 6th mode — general FIFO queueing; processes messages sequentially, no cancellation |
| ✅ | `steer+backlog` mode | OC variant; cancel current + combine all with steer framing (same behaviour as steer) |
| ✅ | Per-channel queue mode overrides | OC `QueueModeByProvider` — `queue.byChannel.<channel>` in agent config |

---

## Slash Commands

Commands confirmed present in `pyclawops/core/commands.py`:
`start`, `help`, `new`, `reset`, `stop`, `compact`, `status`, `whoami`, `model`, `models`,
`think`, `usage`, `context`, `reload`, `restart`, `config`, `export`, `verbose`, `approve`,
`reboot`, `tts`, `job`, `skills`, `skill`, `subagents`, `queue`

### OpenClaw commands not in PyClawOps

| Status | Command | Notes |
|--------|---------|-------|
| ✅ | `/commands` | Alias for `/help` but outputs a different compact format |
| ✅ | `/allowlist` | Manage channel allowlist at runtime: `add <id>`, `remove <id>`, `list` — mutates live config for telegram/slack |
| ✅ | `/session` | Show session details; set `timeout <min>` and `window <n>` overrides in `session.context` |
| ✅ | `/acp` | ACP session management — FA ACP pass-through via `runner.acp_execute("acp", args)` |
| ✅ | `/focus` | Bind a Telegram topic or Slack thread to a specific agent; stored in `gateway._thread_bindings` |
| ✅ | `/unfocus` | Remove a thread/topic binding |
| ✅ | `/agents` | List thread/topic bindings for the current channel |
| ❌ | `/kill` | Duplicate of `/subagents kill` |
| ❌ | `/steer` | Duplicate of `/subagents send` |
| ✅ | `/debug` | Runtime debug overrides: `show`, `set <key> <value>`, `unset <key>`, `reset` |
| ✅ | `/reasoning` | Toggle reasoning output visibility (`on`/`stream`/`off`); sets `runner.show_thinking` + `session.context["show_thinking"]` |
| ✅ | `/elevated` | Toggle elevated exec approval mode: `on`, `off`, `ask`, `full` |
| ✅ | `/exec` | Set per-session exec defaults: host (`sandbox`/`gateway`/`node`), security level, ask policy |
| ✅ | `/bash` | Run a shell command and send stdout+stderr to the agent as context |
| ✅ | `/activation` | Set group activation mode: `mention` (reply only when mentioned) or `always` |
| ✅ | `/send` | Set send policy for this session: `on`, `off`, `inherit` — enforced in `handle_message()` |
| ⬜ | `/dock-*` | Dynamic per-channel dock commands — route replies to a specific channel (e.g. `/dock-telegram`) |

> **Note on `/think` vs `/reasoning`:** PyClawOps's `/think` sets the FastAgent *thinking budget*
> (how much internal reasoning the model does). OC's `/reasoning` controls whether reasoning
> *output* is shown to the user (`on`/`off`/`stream`). These are different knobs.
> FastAgent exposes reasoning effort via `set_reasoning_effort()` and has a separate
> `/model reasoning` subcommand — neither is wired to a PyClawOps user command.

---

## ACP (Agent Client Protocol)

FastAgent ships a full ACP implementation in `fast_agent/acp/`. PyClawOps uses FastAgent
but does not expose ACP to users. This is a **wiring gap**, not an implementation gap.

### Wiring gaps

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | `ACPAwareMixin` wired into `AgentRunner` | `AgentRunner.acp_execute()` wraps FA's `SlashCommandHandler` via `AgentInstance`; lazily created per runner |
| ✅ | `/acp` gateway slash command | FA ACP pass-through via `runner.acp_execute("acp", args)` |
| ✅ | `AcpConfig` schema block | `acp.enabled` in root config; expand later with dispatch mode, allowedAgents, etc. |

### FastAgent ACP slash handlers not yet wired

FastAgent's `SlashCommandHandler` (in `fast_agent/acp/slash/`) implements these handlers.
PyClawOps has its own parallel implementations for some; others are entirely missing.

| Status | FA Handler | Notes |
|--------|-----------|-------|
| ✅ | `/history` | Load, save, and clear session history — routed to FA `handle_history()` via `acp_execute` |
| ✅ | `/save` / `/load` | FA save/load subcommands available via `/history save [name]` / `/history load [name]` |
| ✅ | `/cards` / `/card` | Agent card management — routed to FA via `runner.acp_execute("cards"/"card", args)` |
| ✅ | `/mcp` | MCP server management at runtime — routed to FA via `runner.acp_execute("mcp", args)` |
| ✅ | `/agent` | Agent introspection / attachment — routed to FA via `runner.acp_execute("agent", args)` |
| ✅ | `/model reasoning` | Set FA reasoning effort (`off`/`minimal`/`low`/`medium`/`high`/`xhigh`) via `/model reasoning` subcommand |
| ✅ | `/model fast` | Toggle FA service tier `fast`/`flex` via `/model fast` subcommand |
| ✅ | `/model verbosity` | Set FA text verbosity level via `/model verbosity` subcommand |
| ✅ | `/model web_search` | Toggle web search on/off via `/model web_search` |
| ✅ | `/model web_fetch` | Toggle web fetch on/off via `/model web_fetch` |
| ✅ | `/model doctor` | Model diagnostics via `/model doctor` |
| ✅ | `/model aliases` / `/model catalog` | List model aliases/catalog — FA subcommands |
| ✅ | `/status system` / `/status auth` | FA `handle_status()` subcommands — routed to FA via `runner.acp_execute("status", args)` |
| ✅ | `/clear [last]` | Clear history or undo last turn — routed to FA `handle_clear()` via `acp_execute` |

---

## Config / Schema Gaps

### Gateway Config (`gateway:` block)

PyClawOps currently has: `host`, `port`, `mcp_port`, `debug`, `log_level`, `log_retention_days`,
`webhook_url`, `cors_origins`, `skills_dirs`

| Status | Feature | Notes |
|--------|---------|-------|
| ⬜ | `gateway.auth` | Gateway user auth: modes `none`/`token`/`password`/`trusted-proxy`; rate limiting (`maxAttempts`, `windowMs`, `lockoutMs`); Tailscale auth integration |
| ⬜ | `gateway.tls` | `enabled`, `autoGenerate` (self-signed), `certPath`, `keyPath`, `caPath` (for mTLS) |
| ⬜ | `gateway.tailscale` | Tailscale Serve/Funnel integration: mode `off`/`serve`/`funnel` |
| ⬜ | `gateway.remote` | Connect to a remote gateway: `url`, `transport`, `token`, `tlsFingerprint` |
| ⬜ | `gateway.controlUi` | Embedded web control panel: `enabled`, `basePath`, `allowedOrigins`, `auth` |

### Agent Config (`agents.<name>:` block)

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | Per-agent heartbeat / pulse | Each agent has its own `__pulse__` job in `~/.pyclawops/agents/{name}/jobs.yaml` with independent schedule, message, and delivery config |
| ⬜ | Model fallback chain | Ordered list of fallback models per agent; see Model Fallback section |

### Channel Config

| Status | Feature | Notes |
|--------|---------|-------|
| ⬜ | `DiscordConfig` — allowed_users, streaming, typing_indicator | Discord schema is minimal (token + guilds only); missing allowlist/denylist and all config parity with Telegram |
| ⬜ | `SlackConfig` — pulse_channel wired to schema | `SlackConfig.pulse_channel` exists and pulse sends heartbeat, but Slack pulse is not schema-documented consistently |
| ⬜ | Signal channel in config schema | Adapter exists at `pyclawops/channels/signal.py` but not in `ChannelsConfig` schema |
| ⬜ | iMessage channel in config schema | Adapter exists at `pyclawops/channels/imessage.py` but not in `ChannelsConfig` schema |
| ⬜ | Google Chat channel in config schema | Adapter exists at `pyclawops/channels/googlechat.py` but not in `ChannelsConfig` schema |
| ⬜ | LINE channel in config schema | Adapter exists at `pyclawops/channels/line.py` but not in `ChannelsConfig` schema |
| ⬜ | MS Teams channel | OC has MS Teams support; PyClawOps has no adapter or schema |
| ⬜ | IRC channel | OC has IRC support; PyClawOps has no adapter or schema |
| ⬜ | WebChat channel | OC has a browser/web channel; PyClawOps has no adapter or schema |
| ⬜ | WhatsApp — full wiring | Schema (`WhatsAppConfig`) and adapter (`pyclawops/channels/whatsapp.py`) exist but channel is not wired into Gateway |
| ⬜ | Discord — full wiring | Schema (`DiscordConfig`) and adapter (`pyclawops/channels/discord.py`) exist but channel is not wired into Gateway |
| ⬜ | Per-channel queue mode overrides in schema | OC `QueueModeByProvider` block inside queue config |

### FastAgent Config Not Exposed

These are FastAgent settings that have no corresponding PyClawOps schema field:

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | `agents.<name>.reasoning_effort` | FA `ReasoningEffortSetting` (`off`/`minimal`/`low`/`medium`/`high`/`xhigh`) — wired in schema + `AgentRunner._apply_fa_model_settings()` |
| ✅ | `agents.<name>.text_verbosity` | FA `TextVerbosityLevel` — wired in schema + runner; applied after `__aenter__` |
| ✅ | `agents.<name>.service_tier` | FA service tier `fast`/`flex` per agent — wired in schema + runner |
| ⬜ | MCP server OAuth2 config | FA `MCPServerAuthSettings`: `oauth`, `redirect_port`, `redirect_path`, `scope`, `persist` (`keyring`/`memory`) |
| ⬜ | MCP elicitation mode | FA `MCPElicitationSettings`: `mode` (`forms`/`auto-cancel`/`none`) |
| ⬜ | Shell runtime settings | FA `ShellSettings`: `timeout_seconds`, `interactive_use_pty`, `output_display_lines`, `write_text_file_mode`, etc. |

---

## Hook Events

| Status | Event | Notes |
|--------|-------|-------|
| ✅ | `gateway:startup`, `gateway:shutdown` | |
| ✅ | `message:received`, `message:sent` | |
| ✅ | `command:*`, `session:created`, `session:expired` | |
| ✅ | `agent:after_response`, `tool:before_exec`, `tool:after_exec` | |
| ✅ | `memory:*` (interceptable) | |
| ✅ | `agent:bootstrap` | Fires when a new session runner is created; payload: `agent_id`, `session_id`, `workspace_dir`, `bootstrap_files` (list of paths that exist) |
| ✅ | `message:transcribed` | Constant added (`message:transcribed`); fires when voice input support is added — no voice input path exists yet |
| ✅ | `message:preprocessed` | Fires after all checks (dedup, activation_mode), before agent dispatch; payload: `body_for_agent`, `channel`, `sender_id`, `session_id`, `agent_id`, `transcript` |

---

## MCP Tools

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | All current tools: bash, memory, todos, sessions, jobs, config, skills, subagents, etc. | |
| ⬜ | `send_message` for Slack / Discord | Currently Telegram-only |

---

## Bootstrap / Agent Files

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | `AGENTS.md`, `PERSONALITY.md`, `IDENTITY.md`, `RULES.md`, `USER.md`, `SOUL.md`, `BOOTSTRAP.md`, `MEMORY.md` | |
| ✅ | Per-agent `PULSE.md` | Each agent has its own workspace dir; `__pulse__` job reads that agent's `PULSE.md` instructions |

---

## Focus / Unfocus (Thread Binding)

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | `/focus` — bind Discord thread or Telegram topic to an agent session | `gateway._thread_bindings["{channel}:{thread_id}"] = agent_id`; checked in `_handle_telegram_message` + `_handle_slack_message` |
| ✅ | `/unfocus` — remove thread/topic binding | |
| ⬜ | Thread binding persistence | OC `ThreadBindingsConfig` with `idleHours` TTL; currently in-memory only (lost on restart) |
| ⬜ | Session binding service | Persistent map of `channel+conversationId → sessionKey` |

---

## Model Fallback Chain

| Status | Feature | Notes |
|--------|---------|-------|
| ✅ | Config: ordered list of fallback models per agent | `agents.<name>.fallbacks: [model-a, model-b, ...]` — `AgentConfig.fallbacks` field |
| ✅ | Runtime fallback tracking | `_handle_with_fastagent()` tries next model on error; `session.context["_fallback_index"]` persists across messages |
| ✅ | Fallback notice delivered to user | "↪️ Model Fallback: {next} (tried {effective}; {reason})" prepended to first fallback response |
| ✅ | `/models fallbacks` subcommands | `list`, `add <model>`, `remove <model>`, `clear` — mutates `agent.config.fallbacks` live |

---

## Provider Auth Chain (not gateway auth)

OC `auth.profiles` — how the gateway authenticates to LLM providers. FastAgent already
handles multi-provider auth programmatically via `AgentRunner._build_fa_settings()`. This is a config UX gap, not
a functional one — PyClawOps exposes providers via `providers:` block.

| Status | Feature | Notes |
|--------|---------|-------|
| ⬜ | Named auth profiles per provider | OC `auth.profiles`: named profile with `provider`, `mode` (`api_key`/`oauth`/`token`) |
| ⬜ | Per-provider fallback order | OC `auth.order`: which profile to try first per provider |
| ⬜ | Billing cooldowns | OC `auth.cooldowns`: billing backoff hours per provider, failure window |

---

## Misc / OC-Only Features

| Status | Feature | Notes |
|--------|---------|-------|
| ⬜ | Usage cost tracking | OC tracks cost per message (input/output token costs × price); PyClawOps tracks counts only |
| ⬜ | `/usage cost` / cost summary | Cost reporting in `/usage` command |
| ⬜ | Bedrock model auto-discovery | `BedrockDiscoveryConfig` — scan AWS Bedrock for available models and register them |
| ⬜ | TTS voice chat (`TalkConfig`) | Real-time voice: provider, voiceId, interrupt-on-speech, silence timeout |
| ⬜ | Canvas hosting | Embedded web canvas for rich output rendering |
| ⬜ | Pinned sessions (FA native) | FA `SessionManager` supports `is_session_pinned()` — pinned sessions are never culled by the TTL reaper |
| ⬜ | Session history window (FA native) | FA `session_history_window` setting (default 20) limits how many sessions are listed; PyClawOps has no equivalent |
| ✅ | FastAgent config programmatically injected | `AgentRunner._build_fa_settings()` — constructs FA `Settings` object at runtime from `pyclawops_config`; no `fastagent.config.yaml` needed |
