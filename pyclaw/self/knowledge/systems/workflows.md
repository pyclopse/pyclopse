# Workflows

**Files:** `pyclaw/workflows/chain.py`, `pyclaw/workflows/parallel.py`,
`pyclaw/workflows/agents_as_tools.py`

Higher-level multi-agent patterns built on `AgentRunner`.

---

## ChainWorkflow

Sequential: each agent's output becomes the next agent's input.

```
initial_message → agent_0 → response_0 → agent_1 → response_1 → ... → final
```

Use case: research → summarize → format pipeline.

---

## ParallelWorkflow

Fan-out/fan-in: multiple agents run concurrently, results aggregated.

```
            → specialist_0 → result_0 ─┐
message →   → specialist_1 → result_1 ─┼→ aggregator → final
            → specialist_2 → result_2 ─┘
```

Use case: multi-perspective analysis with synthesis.

---

## AgentsAsTools

Orchestrator agent with sub-agents exposed as callable MCP tools. The
orchestrator decides which specialists to invoke and in what order.

Use case: coordinator that delegates to domain experts on demand.

---

## Workflows vs Subagents

| | Workflows | Subagents |
|---|---|---|
| Execution | Synchronous (await result) | Asynchronous (background) |
| Result | Returned immediately | Delivered via report_to_session |
| Use case | Pipeline, need result now | Background task, result arrives later |
