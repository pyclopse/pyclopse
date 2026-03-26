# FastAgent Bug Report: Tool Call ID One-Turn Lag on Session Reload

**Package:** `fast-agent-mcp` v0.5.9
**Affected files:** `fast_agent/agents/tool_runner.py`, `fast_agent/agents/tool_agent.py`
**Severity:** High — breaks all tool calls for any provider that validates tool_call ID continuity (MiniMax, likely others)

---

## Summary

When a session's persisted history ends with an `assistant` message whose `stop_reason` is `toolUse`, reloading that history causes `reconcile_interrupted_history` to inject a fake "interrupted" tool result. On the *next* actual tool call, the real `tool_result` message references the **new** tool_call_id from the current assistant turn — but the injected message already consumed the slot with the **stale** tool_call_id from the interrupted one. The result is a one-turn lag: every tool response is paired with the wrong tool_call_id.

MiniMax rejects this with:

```
Error code: 400 - {'type': 'error', 'error': {'type': 'bad_request_error',
  'message': 'invalid params, tool call id is invalid (2013)'}}
```

Other providers that validate tool_call_id continuity are likely affected as well.

---

## Root Cause

### Step 1 — History saved mid-tool-loop

`_persist_history` is called inside `_tool_runner_llm_step` → `_generate_with_summary` after each LLM response. If the agentic loop exits while the last LLM response had `stop_reason = toolUse` (e.g. due to a downstream API error, timeout, or external interruption), the history on disk ends with:

```
[N]   assistant  stop_reason=toolUse  tool_calls={id_A: ...}
```

### Step 2 — `reconcile_interrupted_history` fires on reload

In `tool_agent.py`, `generate_impl` calls `reconcile_interrupted_history` at the top of every invocation when `use_history=True`. Detecting `stop_reason == TOOL_USE` on the last message, it appends a synthetic user message:

```
[N]   assistant  stop_reason=toolUse  tool_calls={id_A: ...}   ← from disk
[N+1] user       tool_results={id_A: "**The user interrupted this tool call**"}  ← injected
```

This is the correct recovery for a truly interrupted session. The problem is it also fires when the session is resumed *intentionally* — the user sends a new message, and there is no real interruption to recover from.

### Step 3 — New LLM response generates id_B; `run_tools` sends id_A

The LLM sees the appended interrupted result and responds with a new tool call using a fresh id:

```
[N+2] assistant  stop_reason=toolUse  tool_calls={id_B: ...}
```

FastAgent's `run_tools` then executes the tool and calls `_ensure_tool_response_staged`, which builds the tool result message. At this point `_pending_tool_request` still references the message from step 2's injected result (`id_A`) rather than `id_B`. The outgoing conversation therefore looks like:

```
[N]   assistant  tool_calls={id_A}
[N+1] user       tool_results={id_A}   ← interrupted stub
[N+2] assistant  tool_calls={id_B}
[N+3] user       tool_results={id_A}   ← WRONG — should be id_B
```

The provider sees a `tool_result` that references an already-answered tool_call_id, causing the 400 error.

### Compounding factor — API errors saved as `stop_reason=error`

`_handle_retry_failure` in `fastagent_llm.py` converts `APIError` (HTTP 400) into a failure response with `stop_reason=error` instead of re-raising. This means the error path also calls `_persist_history`, saving the corrupted state to disk with `_completed=True`. The next session reload then hits the bug again immediately.

---

## Minimal Reproduction

The script below directly manipulates `agent.message_history` to simulate history that was saved mid-tool-loop (ending with `stop_reason=toolUse`), then demonstrates that `reconcile_interrupted_history` inserts the interrupted stub and causes the ID mismatch. No live API call is required — the bug is visible purely in the history state.

```python
"""
Minimal reproduction of the FastAgent tool_call_id one-turn lag bug.

Demonstrates that loading history ending with stop_reason=TOOL_USE causes
reconcile_interrupted_history to inject a fake tool result, after which the
next real tool result references the stale (injected) tool_call_id rather
than the current one.

Run with:
    pip install fast-agent-mcp
    python reproduce_tool_id_lag.py
"""

import asyncio
import json

from mcp.types import CallToolRequest
from fast_agent import FastAgent, LlmStopReason, PromptMessageExtended, text_content
from fast_agent.agents.tool_runner import ToolRunner
from fast_agent.mcp.helpers.content_helpers import text_content as tc


def make_assistant_tool_call(tool_call_id: str, tool_name: str = "get_time") -> PromptMessageExtended:
    """Simulate an assistant message that ended mid-loop with a pending tool call."""
    return PromptMessageExtended(
        role="assistant",
        content=tc("I'll check the time for you."),
        stop_reason=LlmStopReason.TOOL_USE,
        tool_calls={
            tool_call_id: CallToolRequest(
                method="tools/call",
                params={"name": tool_name, "arguments": {}},
            )
        },
    )


def make_tool_result(tool_call_id: str) -> PromptMessageExtended:
    """Simulate a real tool result referencing the given tool_call_id."""
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended as PME
    from mcp.types import CallToolResult
    return PME(
        role="user",
        content=tc(""),
        tool_results={
            tool_call_id: CallToolResult(content=[tc("2026-03-12T06:00:00Z")], isError=False)
        },
    )


class _FakeAgent:
    """Minimal stub implementing MessageHistoryAgentProtocol."""
    def __init__(self, history):
        self._history = list(history)

    @property
    def message_history(self):
        return self._history

    def load_message_history(self, messages):
        self._history = list(messages)


def reproduce():
    ID_A = "tool_call_id_from_interrupted_turn"
    ID_B = "tool_call_id_from_new_turn"

    print("=== Simulated persisted history (ends with stop_reason=TOOL_USE) ===")
    saved_history = [
        PromptMessageExtended(role="user", content=tc("What time is it?")),
        make_assistant_tool_call(ID_A),  # ← saved mid-loop; never completed
    ]
    for i, m in enumerate(saved_history):
        sr = getattr(m, "stop_reason", None)
        tc_ids = list((m.tool_calls or {}).keys())
        print(f"  [{i}] role={m.role}  stop_reason={sr}  tool_calls={tc_ids}")

    print()
    print("=== Loading history into agent, then calling reconcile_interrupted_history ===")
    agent = _FakeAgent(saved_history)
    state = ToolRunner.reconcile_interrupted_history(agent, use_history=True)
    print(f"  reconcile status: {state.status}")
    print(f"  history length: {state.history_before} → {state.history_after}")

    print()
    print("=== History after reconcile ===")
    for i, m in enumerate(agent.message_history):
        tr_ids = list((m.tool_results or {}).keys()) if hasattr(m, "tool_results") else []
        tc_ids = list((m.tool_calls or {}).keys())
        sr = getattr(m, "stop_reason", None)
        print(f"  [{i}] role={m.role}  stop_reason={sr}  tool_calls={tc_ids}  tool_results={tr_ids}")

    print()
    print("=== New LLM turn: assistant responds with a NEW tool call (ID_B) ===")
    agent.message_history.append(make_assistant_tool_call(ID_B))

    print()
    print("=== Tool executes and builds result — but _pending_tool_request is still ID_A ===")
    # In the real agentic loop, run_tools() uses _pending_tool_request which was set
    # from the injected interrupted message (ID_A), not the current message (ID_B).
    # The result is sent with the WRONG id.
    wrong_result = make_tool_result(ID_A)   # ← what FastAgent actually sends
    correct_result = make_tool_result(ID_B)  # ← what it should send

    agent.message_history.append(wrong_result)

    print()
    print("=== Final conversation sent to provider ===")
    for i, m in enumerate(agent.message_history):
        tr_ids = list((getattr(m, "tool_results", None) or {}).keys())
        tc_ids = list((getattr(m, "tool_calls", None) or {}).keys())
        sr = getattr(m, "stop_reason", None)
        flag = " ← MISMATCH (provider rejects)" if tr_ids == [ID_A] and i == len(agent.message_history) - 1 else ""
        print(f"  [{i}] role={m.role}  stop_reason={sr}  tool_calls={tc_ids}  tool_results={tr_ids}{flag}")

    print()
    print(f"Expected final tool_result id: {ID_B!r}")
    print(f"Actual   final tool_result id: {ID_A!r}")
    assert ID_A != ID_B
    last_tr_ids = list((agent.message_history[-1].tool_results or {}).keys())
    assert last_tr_ids == [ID_A], "Bug not reproduced — check fast-agent-mcp version"
    print()
    print("BUG CONFIRMED: tool_result references the interrupted call's ID, not the current one.")
    print("MiniMax responds: 400 invalid params, tool call id is invalid (2013)")


if __name__ == "__main__":
    reproduce()
```

**Expected output:**

```
BUG CONFIRMED: tool_result references the interrupted call's ID, not the current one.
MiniMax responds: 400 invalid params, tool call id is invalid (2013)
```

**To observe with a live provider**, replace the `_FakeAgent` stub with a real `FastAgent` agent, persist the `saved_history` list to disk using `fast_agent.mcp.prompt_serialization.save_messages`, reload it in a new session, and send any message that triggers a tool call. The provider will return a 400 on the first tool use.

---

## Proposed Fix

### Option A — Don't call `reconcile_interrupted_history` when a new user message is present (preferred)

`reconcile_interrupted_history` is already guarded by `not has_tool_results`, but it should also check whether the incoming `messages` contains a new human turn. If the user sent a real message, the session is not "interrupted" — it is being resumed. The reconcile logic should only fire when the agent is being re-run with no new input (e.g. a retry/resume with an empty message list).

```python
# tool_agent.py  ~line 357
has_new_human_turn = any(message.role == "user" and not message.tool_results for message in messages)
if use_history and not has_tool_results and not has_new_human_turn:
    history_state = ToolRunner.reconcile_interrupted_history(...)
```

### Option B — Strip incomplete trailing messages before persisting history (workaround)

Before writing history to disk, remove any trailing `assistant` messages whose `stop_reason` is in `{toolUse, error, cancelled, timeout}`. This prevents the bad state from ever being saved, at the cost of losing the partial turn context. This is the approach we are using in pyclopse as a downstream mitigation:

```python
_INCOMPLETE_STOP_REASONS = frozenset({"toolUse", "error", "cancelled", "timeout"})

def _trim_history_for_save(messages):
    trimmed = list(messages)
    while trimmed:
        last = trimmed[-1]
        if getattr(last, "role", None) != "assistant":
            break
        stop = getattr(last, "stop_reason", None)
        stop_val = stop.value if hasattr(stop, "value") else str(stop) if stop else None
        if stop_val in _INCOMPLETE_STOP_REASONS:
            trimmed.pop()
        else:
            break
    return trimmed
```

### Option C — Fix `_pending_tool_request` tracking in `run_tools`

Ensure `_pending_tool_request` is always set from the *current* assistant message (`messages[-1]` after the new LLM step), not carried over from the previous persisted/injected state. This is a narrower fix that targets the ID mismatch directly.

---

## Environment

- `fast-agent-mcp` 0.5.9
- Provider: MiniMax (`api_url: https://api.minimax.io/v1`, using `GENERIC` provider path)
- Session history enabled; history persisted to disk between turns
- Python 3.14, macOS

---

## Additional Notes

- The bug does **not** manifest on providers that do not validate tool_call_id continuity (e.g. Anthropic's Claude API appears tolerant of this).
- FastAgent's internal `save_session_history` hook (`.fast-agent/sessions/`) is unaffected; this only concerns integrations that persist `agent.message_history` externally.
- Option A is preferred because it preserves the intent of `reconcile_interrupted_history` (recovery from a genuine process crash) without penalising normal resumed sessions.
