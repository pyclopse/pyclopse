---
name: boot-md
description: Run BOOT.md instructions through the agent when the gateway starts
version: 1.0.0
events:
  - gateway:startup
handler: handler.py
---

# boot-md

Executes the contents of `BOOT.md` (in the agent workspace or
`~/.pyclopse/BOOT.md`) as an agent message immediately after the gateway
starts and all channels are connected.

Useful for:
- Running startup diagnostics
- Sending a "I'm online" notification via a channel
- Kicking off an initial heartbeat

## BOOT.md location

The handler looks for `BOOT.md` in this order:
1. `~/.pyclopse/BOOT.md`
2. `~/BOOT.md`

## Enable

```
pyclopse hooks enable boot-md
```
