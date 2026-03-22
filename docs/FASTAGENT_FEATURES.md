# FastAgent Features — PyClaw Implementation Guide

This document captures FastAgent capabilities that PyClaw does not yet use, researched in full against
`~/github/fastagent` source and the installed venv. Each section is self-contained so it can be picked
up as an independent implementation task.

---

## Currently Used (Baseline)

PyClaw uses these FastAgent features today:

| Feature | Where |
|---|---|
| `@fast.agent` decorator | `AgentRunner` / `FastAgentFactory` |
| `@fast.chain` / `@fast.parallel` / `@fast.router` | `FastAgentFactory` (optional workflows) |
| `agent.send()` | Turn-based execution |
| `agent.add_stream_listener()` | Streaming chunks (`is_reasoning` flag) |
| `agent.load_message_history()` / `agent.append_history()` | History persistence |
| `Settings` / `MCPServerSettings` / `RequestParams` | Programmatic config in `_build_fa_settings()` |
| `prompt_serialization` (load/save messages) | History JSON I/O |
| `SlashCommandHandler` | ACP slash commands |
| `llm.set_reasoning_effort()` / `set_text_verbosity()` / `set_service_tier()` | Model-level settings |
| `parse_reasoning_setting()` | Reasoning effort parsing |

---

## Feature 1: `@fast.orchestrator` / `@fast.iterative_planner`

### What it is
The most powerful workflow type in FastAgent. Unlike `router` (which picks one agent) or `chain` (fixed
sequence), an orchestrator **dynamically plans** which agents to call and in what order, using an inner
LLM reasoning loop. `iterative_planner` is a variant that refines the plan across multiple passes.

### FastAgent API
```python
@fast.orchestrator(
    name="planner",
    instruction="You coordinate sub-agents to answer complex questions",
    agents=["researcher", "writer", "critic"],   # child agents by name
    plan_type="full",       # "full" (upfront) or "iterative" (step-by-step)
    plan_iterations=5,      # max planning iterations
    model="sonnet",
    request_params=RequestParams(maxTokens=4096),
)
async def planner(): pass

@fast.iterative_planner(
    name="refiner",
    agents=["drafter", "reviewer"],
)
async def refiner(): pass
```

### Config schema addition needed
```yaml
# agent config block
workflow: orchestrator        # or iterative_planner
agents: [researcher, writer]  # child agent names
plan_type: full               # full | iterative
plan_iterations: 5
```

### PyClaw integration points
- `pyclaw/agents/factory.py` — add `orchestrator` and `iterative_planner` cases to `_build_workflow()`
- `pyclaw/config/schema.py` — add `plan_type: Optional[str]` and `plan_iterations: Optional[int]` to `AgentConfig`
- Tests: `tests/test_fa_model_settings.py` or new `tests/test_orchestrator_workflow.py`

### Key difference from router
Router picks one agent per message. Orchestrator breaks work across multiple agents, passes results
between them, and synthesises a final answer — all driven by the orchestrator LLM itself.

---

## Feature 2: `@fast.evaluator_optimizer`

### What it is
A two-agent loop: a **generator** produces a response, an **evaluator** scores it, and if the score is
below `min_rating` the generator is asked to refine. Continues until the rating threshold is met or
`max_refinements` is exhausted.

### FastAgent API
```python
@fast.evaluator_optimizer(
    name="quality_loop",
    generator="drafter",         # agent that writes
    evaluator="critic",          # agent that scores
    min_rating="GOOD",           # EXCELLENT | GOOD | FAIR | POOR
    max_refinements=3,
    refinement_instruction="Please improve based on the critique above.",
)
async def quality_loop(): pass
```

### Config schema addition needed
```yaml
workflow: evaluator_optimizer
generator: drafter
evaluator: critic
min_rating: GOOD          # EXCELLENT | GOOD | FAIR | POOR
max_refinements: 3
refinement_instruction: "Improve based on feedback."
```

### PyClaw integration points
- `pyclaw/agents/factory.py` — add `evaluator_optimizer` case
- `pyclaw/config/schema.py` — add `generator`, `evaluator`, `min_rating`, `max_refinements`, `refinement_instruction` fields

### Use cases in PyClaw
- Job outputs that need quality gating before delivery
- Agent responses that benefit from self-critique (long-form writing, code review)

---

## Feature 3: `@fast.maker` (K-voting)

### What it is
Runs a `worker` agent `k` times independently and selects the consensus answer by voting. Designed for
reliable classification, extraction, or any task where correctness matters more than speed.

### FastAgent API
```python
@fast.maker(
    name="reliable_classifier",
    worker="classifier_agent",
    k=3,                         # votes required for consensus
    max_samples=50,              # give up after this many samples
    match_strategy="normalized", # "exact" | "normalized" | "structured"
    red_flag_max_length=500,     # discard responses longer than this
)
async def reliable_classifier(): pass
```

### Config schema addition needed
```yaml
workflow: maker
worker: classifier_agent
k: 3
max_samples: 50
match_strategy: normalized
```

### PyClaw integration points
- `pyclaw/agents/factory.py` — currently listed as legacy, should be a first-class `workflow:` value
- `pyclaw/config/schema.py` — add `k`, `max_samples`, `match_strategy`, `red_flag_max_length`

---

## Feature 4: Structured Outputs / `response_format`

### What it is
Pass a Pydantic model as `response_format` to get schema-enforced JSON responses from the LLM. Works
via `structured_output_mode: "json" | "tool_use" | "auto"` on the provider settings.

### FastAgent API
```python
from pydantic import BaseModel

class JobSummary(BaseModel):
    title: str
    status: str
    next_run: str | None

result_text = await agent.send("summarise current jobs", response_format=JobSummary)
# result_text is valid JSON matching JobSummary schema
```

Or at the agent level:
```python
@fast.agent(
    name="extractor",
    request_params=RequestParams(response_format=MySchema),
)
```

### PyClaw integration points
- `AgentRunner.run()` / `run_stream()` — pass optional `response_format` parameter through to FA
- `pyclaw/config/schema.py` — `request_params` already accepts arbitrary dicts; could add typed `response_schema` field
- Immediate use: job status tool responses, config validation responses

---

## Feature 5: `function_tools` — Python Functions as Tools

### What it is
Register plain Python callables directly on an agent without needing an MCP server roundtrip. Supports
both inline functions and `"module.py:function_name"` string specs.

### FastAgent API
```python
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return requests.get(f"https://api.weather.com/{city}").text

@fast.agent(
    name="assistant",
    function_tools=[get_weather, "tools/custom.py:my_helper"],
)
async def assistant(): pass
```

### PyClaw integration points
- `pyclaw/agents/runner.py` — pass `function_tools` list to `@fast.agent()` decorator in `initialize()`
- `pyclaw/config/schema.py` — add `function_tools: Optional[List[str]]` (string specs) to `AgentConfig`
- Use case: lightweight per-agent utilities that don't belong in the global MCP server

---

## Feature 6: FastAgent Tool Hooks

### What it is
FastAgent's native per-turn hook system, providing rich execution context that PyClaw's own hook system
doesn't have. Hooks fire on `before_llm_call`, `after_llm_call`, and `after_turn_complete`.

### FastAgent API
```python
from fast_agent.hooks import HookContext

async def usage_logger(ctx: HookContext) -> None:
    if ctx.hook_type == "after_turn_complete":
        print(f"Turn {ctx.iteration}: {ctx.usage.total_tokens} tokens")

@fast.agent(
    name="assistant",
    request_params=RequestParams(
        tool_hooks=ToolHooksConfig(after_turn_complete=[usage_logger])
    ),
)
```

### `HookContext` fields available
- `runner` — tool runner, iteration count
- `agent` — current agent instance
- `message` — latest message
- `hook_type` — which hook fired
- `iteration` — current tool-call iteration
- `is_turn_complete` — whether turn finished
- `message_history` — full history
- `usage` — `UsageAccumulator` with token counts
- `request_params` — live LLM parameters
- `agent_registry` — access other agents

### PyClaw integration points
- `pyclaw/agents/runner.py` — wire token usage from `HookContext.usage` back into `gateway._usage`
- Could replace or augment `pyclaw/hooks/events.py` for per-turn callbacks
- Useful for: per-turn token tracking, rate limiting, audit logging

---

## Feature 7: Native Anthropic Web Search / Web Fetch

### What it is
First-class Anthropic-side web search and web fetch, configured per provider — no subprocess or external
MCP server required. Replaces `mcp-server-fetch`.

### FastAgent config (`fastagent.config.yaml` or `Settings`)
```yaml
anthropic:
  web_search:
    enabled: true
    max_uses: 10
    allowed_domains: ["docs.python.org", "github.com"]
    blocked_domains: []
    user_location:
      country: US
      city: New York
  web_fetch:
    enabled: true
    max_uses: 5
    citations_enabled: true
    max_content_tokens: 8192
```

### Programmatic (in `AgentRunner._build_fa_settings()`)
```python
from fast_agent.config import AnthropicSettings, AnthropicWebSearchSettings

anthropic_settings = AnthropicSettings(
    api_key=api_key,
    web_search=AnthropicWebSearchSettings(enabled=True, max_uses=10),
    web_fetch=AnthropicWebFetchSettings(enabled=True, citations_enabled=True),
)
```

### PyClaw integration points
- `pyclaw/agents/runner.py` `_build_fa_settings()` — add web_search/web_fetch to AnthropicSettings when configured
- `pyclaw/config/schema.py` — add `web_search: bool` and `web_fetch: bool` (or full settings) to `AgentConfig`
- `fastagent.config.yaml` — update example config
- Allows removing `fetch` from default MCP servers for Anthropic-backed agents

---

## Feature 8: Instruction Templates

### What it is
FastAgent instruction strings support runtime token substitution. These are resolved when the agent
is first used, keeping instruction definitions clean and declarative.

### Supported tokens
| Token | Resolves to |
|---|---|
| `{{currentDate}}` | Current date as "DD Month YYYY" |
| `{{url:https://...}}` | Fetches URL content at runtime |
| `{{file:/path/to/file}}` | Reads file contents at runtime |
| `{{file_silent:/path}}` | Reads file silently (no log noise) |

### Example
```python
@fast.agent(
    instruction="""
    You are a helpful assistant. Today is {{currentDate}}.
    Project guidelines: {{file:~/.pyclaw/GUIDELINES.md}}
    """,
)
```

### PyClaw integration points
- `pyclaw/core/prompt_builder.py` — currently builds instructions manually; could delegate token
  substitution to FastAgent by passing raw template strings with `{{}}` tokens instead of
  pre-resolving them in Python
- `{{file:}}` token is equivalent to the `boot-md` hook today — could simplify that hook
- No code change required to use these — just put tokens in agent instruction strings in config

---

## Feature 9: Prompt Caching (Anthropic)

### What it is
Controls Anthropic's prompt caching behaviour. Reducing redundant token processing on repeated system
prompts / tool definitions.

### FastAgent config
```yaml
anthropic:
  cache_mode: auto      # "off" | "prompt" | "auto"
  cache_ttl: 5m         # "5m" | "1h"
```

### PyClaw integration points
- `pyclaw/agents/runner.py` `_build_fa_settings()` — expose `cache_mode` via `AnthropicSettings`
- `pyclaw/config/schema.py` — add `cache_mode: Optional[str]` to provider config or `AgentConfig`
- FastAgent likely defaults to `"auto"` already; explicit config lets us tune per-agent

---

## Feature 10: `trim_tool_history`

### What it is
FastAgent-native flag to strip internal tool call / tool result pairs from the history that is sent to
the LLM on subsequent turns. Reduces token consumption on tool-heavy sessions without losing the
final responses.

### FastAgent API
```python
@fast.agent(
    name="assistant",
    request_params=RequestParams(trim_tool_history=True),
)
```

### PyClaw notes
PyClaw currently does its own manual history trimming in `_trim_history_for_save()` and
`_purge_corrupted_pairs()`. The FA-native flag operates at inference time (not at save time), so
these are complementary rather than duplicates.

### PyClaw integration points
- `pyclaw/agents/runner.py` — add `trim_tool_history` to `RequestParams` construction when configured
- `pyclaw/config/schema.py` — add `trim_tool_history: bool = False` to `AgentConfig`

---

## Feature 11: OpenTelemetry Tracing

### What it is
FastAgent has built-in OTEL support for distributed tracing of agent execution — spans per turn, per
tool call, with configurable OTLP export.

### FastAgent config
```yaml
otel:
  enabled: true
  service_name: pyclaw
  otlp_endpoint: http://localhost:4317
  console_debug: false
```

### PyClaw integration points
- `pyclaw/agents/runner.py` `_build_fa_settings()` — populate `OpenTelemetrySettings` from pyclaw config
- `pyclaw/config/schema.py` — add `ObservabilityConfig` block with `otel_enabled`, `otlp_endpoint`
- `fastagent.config.yaml` — add commented-out example block

---

## Feature 12: A2A (Agent-to-Agent Protocol) Server

### What it is
Every FastAgent agent already implements `agent_card()` as part of `AgentProtocol`. The card exports
agent name, description, version, capabilities, and all MCP tools as `AgentSkill` objects.
FastAgent ships a complete example A2A server (`examples/a2a/`) that wraps any agent into an HTTP/JSON-RPC
endpoint using `A2AStarletteApplication`.

### FastAgent A2A stack
```
A2A client (external)
    → JSON-RPC HTTP → A2AStarletteApplication (port 8082)
        → DefaultRequestHandler + InMemoryTaskStore
            → FastAgentExecutor.execute()
                → agent.send(message)
                    → LLM + tools
```

### Key classes (from `examples/a2a/`)
```python
# agent_executor.py
class FastAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.get_user_input().strip()
        agent = await self._agent()
        response = await agent.send(message)
        await event_queue.enqueue_event(new_agent_text_message(response))

    async def agent_card(self, agent_name: str | None = None) -> AgentCard:
        agent = await self._agent(agent_name)
        return await agent.agent_card()

# server.py
executor = FastAgentExecutor()
agent_card = _with_server_url(await executor.agent_card())
server = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=DefaultRequestHandler(executor, InMemoryTaskStore()),
)
# uvicorn.run(server.build(), host="0.0.0.0", port=8082)
```

### AgentCard defaults (set by FastAgent automatically)
```python
AgentCard(
    name=agent._name,
    description=agent.config.description or agent.instruction,
    url=f"fast-agent://agents/{agent._name}/",
    version="0.1",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        state_transition_history=False,
    ),
    skills=[AgentSkill(id=tool.name, name=..., description=..., tags=["tool"]) for tool in mcp_tools],
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
)
```

### PyClaw implementation plan
1. **`pyclaw/a2a/executor.py`** — `PyclawA2AExecutor(AgentExecutor)` routing `execute()` → `gateway.dispatch()` or `agent_runner.run()`
2. **`pyclaw/a2a/server.py`** — builds `A2AStarletteApplication` per configured agent, injects server URL
3. **`pyclaw/core/gateway.py`** — start A2A server(s) in `run_gateway()` startup sequence (port 8082+)
4. **`pyclaw/config/schema.py`** — add `a2a.enabled: bool`, `a2a.port: int = 8082`, `a2a.base_url: str` to config
5. **`__main__.py`** — add A2A server to startup sequence after MCP (8081) and REST (8080)

### Task store option
Use `InMemoryTaskStore` initially. Long-term: wire to `JobScheduler` for persistent A2A tasks that survive restarts.

---

## Feature 13: MCP Server Advanced Options (Unused)

Several `MCPServerSettings` fields are available but not configured in PyClaw:

| Field | Default | What it does |
|---|---|---|
| `roots` | None | Scopes filesystem MCP server to specific `file://` URI roots |
| `ping_interval_seconds` | 30 | How often FA pings MCP server to detect disconnect |
| `max_missed_pings` | 3 | Missed pings before reconnect |
| `reconnect_on_disconnect` | True | Auto-reconnect on 404 |
| `http_timeout_seconds` | None | Overall HTTP timeout for MCP calls |
| `read_timeout_seconds` | None | Per-session read timeout |
| `include_instructions` | True | Whether server's instructions are injected into system prompt |
| `elicitation` | None | Forms UI mode (`"forms"` / `"auto-cancel"` / `"none"`) |
| `experimental_session_advertise` | False | Advertise session test capability |

### PyClaw integration points
- `pyclaw/agents/runner.py` `_build_fa_settings()` — wire `roots` to scope the `filesystem` server
- `ping_interval_seconds` / `max_missed_pings` — tune for long-running sessions
- `include_instructions: False` on `pyclaw` server if its instructions are noisy

---

## Feature 14: `human_input=True` on Agents

### What it is
Adds a `human_input` tool to the agent. When the LLM calls it, FastAgent surfaces a `HumanInputRequest`
with a prompt + optional timeout. The response is fed back to the LLM as a tool result.

### FastAgent API
```python
@fast.agent(
    name="approver",
    human_input=True,
)
async def approver(): pass
```

### Custom elicitation handler
```python
async def my_handler(request: HumanInputRequest) -> HumanInputResponse:
    # bridge to Telegram/Slack ask-user flow
    answer = await gateway.ask_user(request.prompt, timeout=request.timeout_seconds)
    return HumanInputResponse(request_id=request.request_id, response=answer)

@fast.agent(name="approver", human_input=True, elicitation_handler=my_handler)
```

### PyClaw integration points
- `pyclaw/agents/runner.py` — pass `human_input=True` + `elicitation_handler=` when agent config enables it
- `pyclaw/config/schema.py` — add `human_input: bool = False` to `AgentConfig`
- Gateway needs a `ask_user()` bridge that sends a message to the originating channel and awaits reply
- Currently PyClaw uses ACP-based approval; FA's native human input is cleaner for in-conversation asks

---

## Feature 15: `model_aliases` in Settings

### What it is
Namespace-scoped model aliases defined in `fastagent.config.yaml` (or `Settings`). Let you refer to
models by short names within a project without hardcoding full provider strings everywhere.

### FastAgent config
```yaml
model_aliases:
  fast: anthropic.claude-haiku-4-5-20251001
  smart: anthropic.claude-sonnet-4-6
  reason: anthropic.claude-opus-4-6.high
```

### PyClaw integration points
- `fastagent.config.yaml` — add `model_aliases` block for PyClaw's standard model tiers
- `pyclaw/agents/runner.py` `_build_fa_settings()` — populate `model_aliases` from pyclaw config
- `pyclaw/config/schema.py` — add `model_aliases: Optional[Dict[str, str]]` to gateway config

---

## Feature 16: `llm_retries`

### What it is
Number of times FastAgent retries a failed LLM call before raising. Currently not configured in PyClaw
(relies on FastAgent's default, which is likely 0 or 1).

### FastAgent config
```yaml
llm_retries: 3
```

### PyClaw integration points
- `pyclaw/agents/runner.py` `_build_fa_settings()` — set `llm_retries` from config
- `pyclaw/config/schema.py` — add `llm_retries: int = 1` to `AgentConfig` or gateway config

---

## Feature 17: `ResponseMode` / `ToolResultMode` (Passthrough)

### What it is
Controls whether tool results are synthesised by the LLM (`"postprocess"`) or returned directly to the
caller (`"passthrough"`). Passthrough is useful for agents that are themselves tools in a larger pipeline.

### FastAgent API
```python
RequestParams(tool_result_mode="passthrough")
```

### PyClaw integration points
- `pyclaw/config/schema.py` — add `tool_result_mode: Optional[str]` to `AgentConfig`
- `pyclaw/agents/runner.py` — pass through to `RequestParams`
- Useful for sub-agents in chain/parallel workflows where the fan-in agent does the synthesis

---

## Feature 18: Multimodal Message Construction

### What it is
FastAgent provides content helpers for constructing multimodal messages beyond plain text:

```python
from fast_agent.messages import image_link, video_link, audio_link, resource_link

await agent.send([
    "Describe this image:",
    image_link("https://example.com/photo.jpg"),
])

await agent.send([
    "Summarise this document:",
    resource_link("mcp://filesystem/README.md"),
])
```

### PyClaw integration points
- `pyclaw/core/gateway.py` — Telegram photo/document messages currently not forwarded to agents
- Would require channel plugins to produce structured content lists instead of plain strings
- `AgentRunner.run()` signature would need to accept `list[str | ContentPart]` not just `str`

---

## Summary Table

| # | Feature | Effort | Value |
|---|---|---|---|
| 1 | `@fast.orchestrator` / `iterative_planner` | Medium | High |
| 2 | `@fast.evaluator_optimizer` | Medium | High |
| 3 | `@fast.maker` K-voting | Low | Medium |
| 4 | Structured outputs (`response_format`) | Low | High |
| 5 | `function_tools` — Python callables as tools | Low | Medium |
| 6 | FastAgent tool hooks (`HookContext`) | Medium | Medium |
| 7 | Native Anthropic web search / web fetch | Low | High |
| 8 | Instruction templates (`{{currentDate}}` etc.) | Low | Low |
| 9 | Prompt caching config (`cache_mode`) | Low | Medium |
| 10 | `trim_tool_history` | Low | Medium |
| 11 | OpenTelemetry tracing | Medium | Medium |
| 12 | A2A server (expose agents externally) | High | High |
| 13 | MCP server advanced options (`roots`, timeouts) | Low | Low |
| 14 | `human_input=True` + elicitation handler | Medium | Medium |
| 15 | `model_aliases` | Low | Low |
| 16 | `llm_retries` | Low | Low |
| 17 | `ResponseMode` / `ToolResultMode` passthrough | Low | Medium |
| 18 | Multimodal message construction | High | Medium |

---

## FastAgent Source Reference Paths

| What | Path |
|---|---|
| All agent decorators | `~/github/fastagent/src/mcp_agent/core/fastagent.py` |
| Agent protocol / `agent_card()` | `~/github/fastagent/src/fast_agent/interfaces.py` |
| `LlmDecorator` base | `~/github/fastagent/src/fast_agent/agents/llm_decorator.py` |
| `McpAgent` + `agent_card()` with skills | `~/github/fastagent/src/fast_agent/agents/mcp_agent.py` |
| `LlmAgent` + `DEFAULT_CAPABILITIES` | `~/github/fastagent/src/fast_agent/agents/llm_agent.py` |
| Orchestrator / iterative planner | `~/github/fastagent/src/fast_agent/workflows/orchestrator.py` |
| Evaluator-optimizer | `~/github/fastagent/src/fast_agent/workflows/evaluator_optimizer.py` |
| Parallel / chain / router | `~/github/fastagent/src/fast_agent/workflows/` |
| Hook system | `~/github/fastagent/src/fast_agent/hooks/` |
| A2A example | `~/github/fastagent/examples/a2a/` |
| Settings / config schema | `~/github/fastagent/src/fast_agent/config.py` |
| RequestParams | `~/github/fastagent/src/fast_agent/mcp/types.py` |
| ACP implementation | `~/github/fastagent/src/fast_agent/acp/` |
| prompt_serialization | `~/github/fastagent/src/fast_agent/mcp/prompt_serialization.py` |
