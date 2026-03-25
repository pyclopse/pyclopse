#!/usr/bin/env python3
"""
boot-md hook handler.

Fires on gateway:startup.  Reads BOOT.md and POSTs its contents to the
gateway REST API as a startup message.

Runs as a subprocess — cannot use in-process singletons like
Gateway.get_instance().  Uses the gateway REST API instead.
"""
import json
import os
import sys
from pathlib import Path


_BOOT_CANDIDATES = [
    Path("~/.pyclawops/BOOT.md").expanduser(),
    Path("~/BOOT.md").expanduser(),
]

_GATEWAY_BASE = os.environ.get("PYCLAW_GATEWAY_URL", "http://localhost:8080")


def main() -> None:
    """Entry point for the boot-md hook handler subprocess.

    Reads a JSON event context from stdin, locates the first existing BOOT.md
    candidate file (``~/.pyclawops/BOOT.md`` then ``~/BOOT.md``), and POSTs its
    contents to the gateway REST API as a system message for the agent named
    in the context.

    The function exits with code 0 on success or when no BOOT.md exists.
    It exits with code 1 and writes to stderr if JSON parsing fails or if the
    HTTP POST to the gateway fails.

    Raises:
        SystemExit: Always — exits 0 on success/no-op, exits 1 on error.
    """
    raw = sys.stdin.read()
    try:
        ctx = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("boot-md: invalid JSON on stdin\n")
        sys.exit(1)

    boot_md: Path | None = None
    for candidate in _BOOT_CANDIDATES:
        if candidate.exists():
            boot_md = candidate
            break

    if boot_md is None:
        # No BOOT.md found — silently skip
        sys.exit(0)

    content = boot_md.read_text(encoding="utf-8").strip()
    if not content:
        sys.exit(0)

    agent = ctx.get("agent", "assistant")

    try:
        import urllib.request
        url = f"{_GATEWAY_BASE}/api/v1/agents/{agent}/messages"
        payload = json.dumps({
            "content": content,
            "channel": "system",
            "session_id": "boot",
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as exc:
        sys.stderr.write(f"boot-md: failed to send to gateway: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
