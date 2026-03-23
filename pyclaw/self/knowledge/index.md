# pyclaw Self-Knowledge Index

Use `self_read('<topic>')` to read any topic. Use `self_source('<module>')` to
read source code. Topics are grouped by category.

---

## overview
What pyclaw is, the full request flow, startup sequence, and how all subsystems
connect. Start here.

---

## architecture/gateway
The Gateway class: main orchestrator, lifecycle (start/stop), message dispatch,
deduplication, channel wiring, Telegram polling, and session routing.

## architecture/sessions
Two-layer session model: routing metadata (Session) + FastAgent history files
on disk. Active session pointer, reaper, daily rollover, /reset, job sessions.

## architecture/agents
Agent dataclass, AgentManager, AgentRunner (wraps FastAgent). Per-session runner
cache, history I/O, thinking tag stripping, model concurrency.

## architecture/channels
ChannelPlugin ABC and GatewayHandle. Telegram (multi-bot, splitting, topics),
Slack (threading, pulse), and the channel plugin discovery system.

## architecture/hooks
Hook system: HookEvent enum, HookRegistry (notify vs intercept patterns),
HookLoader, and bundled hooks (session-memory, boot-md).

## architecture/queue
SessionMessageQueue: modes (followup, collect, interrupt, steer, queue),
debounce, cap, drop policies. How rapid messages are batched.

---

## systems/jobs
Job scheduler: cron/interval/one-shot schedules, AgentRun vs CommandRun,
delivery types, prompt presets, session modes, failure alerting, subagents.

## systems/memory
Memory system: MemoryService, FileMemoryBackend (daily journals + MEMORY.md),
ClawVault, vector search with embeddings, hook intercept pattern.

## systems/skills
Skill system: SKILL.md format, frontmatter fields, discovery paths, template
variables, slash command invocation, system prompt injection, bundled skills.

## systems/security
Security: ExecApprovalSystem (allowlist/denylist/all/none), Docker sandbox,
AuditLogger. How the bash tool enforces policies.

## systems/config
Config system: Pydantic schema, camelCase YAML with validation_alias, secrets
resolution (${env:X}, ${keychain:X}, ${file:X}), loader, and key patterns.

## systems/mcp-tools
The pyclaw MCP server (port 8081): all tools, patterns, lifecycle, logging
middleware, and how agents connect to it via FastAgent.

## systems/api
REST API (port 8080): all routes, patterns, gateway access, and how MCP tools
call back into the gateway via HTTP.

## systems/self
Self-knowledge system: MCP tools (`self_topics`, `self_read`, `self_source`),
REST API mirror, knowledge base layout, DocLoader, config, and startup wiring.

## systems/tui
Dashboard TUI (Textual): layout, tab strip, key bindings, log drain, status
bar, and how it drives the same gateway as headless mode.

## systems/a2a
Agent-to-Agent (A2A) protocol: endpoints, session modes, agent cards, config,
PyclawAgentExecutor, and how external agents call pyclaw agents over HTTP.

## systems/workflows
Multi-agent workflow patterns: ChainWorkflow, ParallelWorkflow, AgentsAsTools.
How they build on AgentRunner.

---

## development/testing
Test patterns: Gateway stubs, async conventions, config validation style,
session/history mocking, concurrency mocking. How the test suite is structured.

## development/conventions
Naming, config style, MCP tool idioms, error handling patterns, file layout
conventions, and what NOT to do.

## development/extending
How to add a new channel adapter, hook handler, MCP tool, provider, or config
section. Extension points and patterns.

## development/release
Versioning (hatch-vcs), tagging, install/update/remove workflow, SSH key
setup, and the release checklist.
