#!/usr/bin/env python3
"""
session-memory hook handler.

Reads event context from stdin (JSON), writes a memory entry for the
outgoing session to the agent's FileMemoryBackend, then exits 0.

Runs as a subprocess — cannot use in-process singletons like
get_memory_service().  Uses FileMemoryBackend directly.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    raw = sys.stdin.read()
    try:
        ctx = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("session-memory: invalid JSON on stdin\n")
        sys.exit(1)

    agent = ctx.get("agent", "default")
    session_id = ctx.get("session_id", "unknown")
    history = ctx.get("data", {}).get("history", [])
    message_count = len(history)

    if message_count == 0:
        # Nothing to save for an empty session
        sys.exit(0)

    entry = {
        "content": (
            f"Session {session_id[:12]} ended via "
            f"{ctx.get('event', 'command:reset')} "
            f"({message_count} messages)"
        ),
        "tags": ["session", agent],
        "agent": agent,
        "session_id": session_id,
        "message_count": message_count,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "recent": history[-5:] if len(history) >= 5 else history,
    }

    key = f"session:{agent}:{session_id}"

    try:
        from pyclaw.memory.file_backend import FileMemoryBackend

        config_dir = os.environ.get("PYCLAW_CONFIG_DIR", "~/.pyclaw")
        agent_dir = str(Path(config_dir).expanduser() / "agents" / agent)
        backend = FileMemoryBackend(base_dir=agent_dir)

        asyncio.run(backend.write(key, entry))
    except Exception as exc:
        sys.stderr.write(f"session-memory: write failed: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
