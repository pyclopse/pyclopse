---
name: session-memory
description: Save session context to memory when /new or /reset is issued
version: 1.0.0
events:
  - command:new
  - command:reset
handler: handler.py
requirements:
  config:
    - sessions.persist_dir
    - memory.clawvault.vault_path
---

# session-memory

Saves a summary of the current session to the memory backend when the user
issues `/new` or `/reset`.  This gives the agent continuity between sessions —
previous context is preserved as a memory entry that can be retrieved later.

## Output

Writes a memory entry keyed by `session:{agent}:{session_id}` containing the
session summary and timestamp.

## Enable

```
pyclaw hooks enable session-memory
```
