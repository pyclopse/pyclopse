# Agents

**Files:** `pyclawops/core/agent.py`, `pyclawops/agents/runner.py`, `pyclawops/agents/factory.py`

---

## Agent Dataclass (`pyclawops/core/agent.py`)

`Agent` is the runtime wrapper around an agent's config. It holds the per-session
runner cache and is the gateway's handle for dispatching messages.

Key attributes:
- `id: str` — matches the key in `config.agents`
- `name: str` — display name from `AgentConfig.name`
- `config: AgentConfig` — full agent configuration
- `_session_runners: dict[str, AgentRunner]` — one runner per active session
- `fast_agent_runner: AgentRunner | None` — base runner (no history path)

Key methods:
- `handle_message(message, session)` — dispatches a turn; returns OutgoingMessage
- `evict_session_runner(session_id)` — closes and removes a runner on error
- `stop()` — cleans up all runners (closes FA MCP connections)

### Session Runner Cache

Each `(agent, session)` pair gets its own `AgentRunner` instance cached in
`agent._session_runners[session_id]`. This preserves per-session conversation
history between turns. Runners are created lazily on first message to a session.

On error in `handle_message()`, `evict_session_runner(session_id)` is called.
The next message creates a fresh runner which reloads history from disk —
clean reconnect while preserving context.

---

## AgentManager (`pyclawops/core/agent.py`)

`AgentManager` owns the `{agent_id: Agent}` map. Created by Gateway during
`initialize()`. All agents from `config.agents` are instantiated at startup.

```python
agent = agent_manager.get_agent(agent_id)
agent = agent_manager.default_agent   # first agent in config
```

---

## AgentRunner (`pyclawops/agents/runner.py`)

`AgentRunner` wraps FastAgent for a single named agent instance. It is the
boundary between pyclawops's session model and FastAgent's execution engine.

### Construction

```python
runner = AgentRunner(
    agent_name="assistant-2026-03-11",   # unique FA agent name
    owner_name="assistant",              # pyclawops agent id
    instruction=system_prompt,           # assembled system prompt
    model="sonnet",
    servers=["pyclawops", "fetch"],         # MCP servers to connect to
    history_path=session.history_path,   # None for isolated job sessions
)
```

### Streaming

`run_stream(messages)` → `AsyncIterator[tuple[str, bool]]`

Yields `(text_chunk, is_reasoning)` tuples. The `is_reasoning` flag is `True`
for content extracted from `<thinking>` tags. The caller (Gateway) renders
reasoning differently — e.g., as Telegram spoiler blocks.

### Thinking Tag Handling

When `show_thinking=False` (default), `<thinking>...</thinking>` blocks are
stripped from the response before yielding to the caller.

When `show_thinking=True`, reasoning content is yielded with `is_reasoning=True`
and the gateway formats it as expandable spoiler text.

Regex: `r"<(thinking|think)>(.*?)</(thinking|think)>"` with DOTALL | IGNORECASE.

### History I/O

History is loaded lazily on first `run()` or `run_stream()` call:

```
_load_history() → FA load_messages(history_path) → agent.load_message_history()
```

History is saved after every successful turn via `_save_history()`. The
`_completed` flag pattern ensures saves only happen on full successful turns:

```python
_completed = False
try:
    result = await self._app.send(prompt)
    _completed = True
    return result
finally:
    if _completed:
        await self._save_history()
```

See `architecture/sessions` for the full history file format and rotation.

### Error Recovery

If FastAgent returns a response starting with `"I hit an internal error"`, a
`RuntimeError` is raised immediately — the response is not saved and the
runner is evicted so the next message gets a clean FA context.

Any trailing `PromptMessageExtended` entries with `stop_reason` in
`{toolUse, error, cancelled, timeout}` are stripped before saving — these
indicate incomplete turns that would corrupt future context.

---

## AgentFactory (`pyclawops/agents/factory.py`)

`FastAgentFactory.create_agent_from_config(agent_config)` builds an
`AgentRunner` from a `AgentConfig`. Key steps:

1. Selects provider (OpenAI, Anthropic, MiniMax, generic) from model string
2. Builds `fastagent.config.yaml`-style settings (`build_fa_settings()`)
3. Constructs `AgentRunner` with the assembled settings

For `generic.*` models (MiniMax), applies a monkey-patch to `OpenAILLM` that
handles `delta.reasoning_details` — needed because MiniMax's streaming format
differs slightly from OpenAI's.

---

## System Prompt Assembly

The system prompt is assembled by `pyclawops/core/prompt_builder.py` from files
in `~/.pyclawops/agents/{agent_id}/`:

| Include flag | File(s) |
|---|---|
| `include_personality` | `PERSONALITY.md`, `SOUL.md` |
| `include_identity` | `IDENTITY.md` |
| `include_rules` | `RULES.md` |
| `include_memory` | `MEMORY.md` |
| `include_user` | `USER.md` |
| `include_agents` | `AGENTS.md` |
| `include_tools` | `TOOLS.md` |
| `include_skills` | `<available_skills>` XML block |

Skills are injected as lean `<available_skills>` XML — only name + description,
not the full skill body. The full body is injected when a skill is invoked.

Subagents skip skill injection to keep their prompts lean.
