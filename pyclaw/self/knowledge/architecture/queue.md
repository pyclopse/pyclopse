# Session Message Queue

**File:** `pyclaw/core/queue.py`

`SessionMessageQueue` manages how rapid inbound messages are batched and
delivered to agents. Each session gets its own queue. The mode controls what
happens when a new message arrives while the agent is already processing.

---

## Modes

| Mode | Behaviour |
|------|-----------|
| `followup` | Process one at a time. New messages wait. After current turn completes, pending messages are processed in order. Default. |
| `collect` | Batch all pending messages into a single dispatch. The agent receives them all at once as one context. |
| `interrupt` | Cancel the current turn. Only the newest message is kept. Backlog is discarded. |
| `steer` | Cancel the current turn. Combine the original message + the correction into one context using a steering frame. |
| `steer-backlog` | Never cancel. After the current turn completes, combine any backlog with the next user message. |
| `steer+backlog` | Alias for `steer-backlog`. |
| `queue` | Strict FIFO. No cancellation, no combining, no debounce. Every message is processed independently in order. |

---

## Debounce

Default: 300ms. After a message arrives, the queue waits up to 300ms for more
messages before dispatching. This prevents chatty users from sending 5 messages
that trigger 5 separate agent turns — the messages are collected and processed
together (depending on mode).

Configurable per-agent via `queue.debounce_ms` in agent config.

`queue` mode ignores debounce — every message dispatches immediately.

---

## Cap and Drop Policy

`cap: int = 20` — maximum messages queued per session before the drop policy
activates.

`drop: "old" | "new" | "summarize"` — what to do when the cap is hit:
- `old` — discard the oldest queued message
- `new` — discard the incoming message
- `summarize` — summarize the backlog into a single message (LLM call)

---

## QueueManager

`QueueManager` owns all `SessionMessageQueue` instances, keyed by session ID.
Created at Gateway startup; queues are created lazily per session.

```python
queue = queue_manager.get_or_create(session_id, mode=agent_config.queue_mode)
await queue.enqueue(message)
async for batch in queue.drain():
    await agent.handle_message(batch, session)
```
