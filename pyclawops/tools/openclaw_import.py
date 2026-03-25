"""Import OpenClaw session history into pyclawops's FastAgent-native format.

Usage (from CLI):
    pyclawops import-openclaw [--agent NAME] [--all] [--openclaw-dir DIR]

OpenClaw stores sessions as JSONL files under:
    ~/.openclaw/agents/{agent}/sessions/{session_id}.jsonl

Each line is a JSON record with a "type" field:
    {"type": "session", ...}
    {"type": "message", "role": "user"|"assistant", "content": "...", ...}
    {"type": "compaction", ...}   (skipped)

Imported sessions land in:
    ~/.pyclawops/agents/{agent}/sessions/{YYYY-MM-DD}-{6chars}/
        session.json      (pyclawops metadata)
        history.json      (FA-native PromptMessageExtended JSON)
"""

from __future__ import annotations

import json
import os
import secrets
import string
from datetime import datetime, timezone
from pyclawops.utils.time import now
from pathlib import Path
from typing import Any, Dict, List, Optional


_SESSION_ALPHABET = string.ascii_letters + string.digits

_DEFAULT_OPENCLAW_DIR = "~/.openclaw"
_DEFAULT_PYCLAW_DIR = "~/.pyclawops"


def _gen_session_id(dt: Optional[datetime] = None) -> str:
    """Generate a date-prefixed session ID with a random 6-character suffix.

    Args:
        dt (Optional[datetime]): Datetime to use for the date prefix. Defaults
            to the current time via ``now()``.

    Returns:
        str: Session ID in the format ``YYYY-MM-DD-XXXXXX`` where ``XXXXXX`` is
        a random alphanumeric suffix.
    """
    d = dt or now()
    suffix = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(6))
    return f"{d.strftime('%Y-%m-%d')}-{suffix}"


def _parse_openclaw_dt(value: Any) -> Optional[datetime]:
    """Parse an OpenClaw ISO datetime string into a UTC-naive datetime.

    Handles both ``Z`` suffix and ``+00:00`` offset notation.  Returns None
    for falsy input or strings that cannot be parsed.

    Args:
        value (Any): ISO 8601 datetime string from an OpenClaw record, or None.

    Returns:
        Optional[datetime]: UTC-naive datetime object, or None if unparseable.
    """
    if not value:
        return None
    try:
        # Handle both "Z" suffix and "+00:00"
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # Normalise to UTC-naive
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Load a JSONL file and return a list of parsed JSON records.

    Skips blank lines and silently drops malformed lines.

    Args:
        path (Path): Path to the ``.jsonl`` file to read.

    Returns:
        List[Dict[str, Any]]: List of parsed JSON record dicts.
    """
    records: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # skip malformed lines
    return records


def _openclaw_session_date(records: List[Dict[str, Any]]) -> Optional[datetime]:
    """Infer the session creation datetime from a list of OpenClaw JSONL records.

    Tries the ``session`` record first (checking ``timestamp``, ``updatedAt``,
    and ``createdAt`` in that order), then falls back to the first ``message``
    record's timestamp.

    Args:
        records (List[Dict[str, Any]]): Parsed JSONL records from one session file.

    Returns:
        Optional[datetime]: UTC-naive datetime for the session, or None if no
        recognisable timestamp is found.
    """
    for rec in records:
        if rec.get("type") == "session":
            # OpenClaw v3 uses "timestamp"; older versions used "updatedAt"/"createdAt"
            dt = (
                _parse_openclaw_dt(rec.get("timestamp"))
                or _parse_openclaw_dt(rec.get("updatedAt"))
                or _parse_openclaw_dt(rec.get("createdAt"))
            )
            if dt:
                return dt
    # Fallback: first message timestamp
    for rec in records:
        if rec.get("type") == "message":
            dt = _parse_openclaw_dt(rec.get("timestamp") or rec.get("createdAt"))
            if dt:
                return dt
    return None


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from an OpenClaw content value.

    Handles both plain string content and Anthropic-style content block lists
    (``text`` and ``tool_result`` blocks).  Tool results are recursively
    flattened to their inner text.

    Args:
        content (Any): An OpenClaw message content value — either a plain string,
            a list of content block dicts, or any other value.

    Returns:
        str: Concatenated plain-text representation of the content.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Flatten tool results to text
                    inner = block.get("content", "")
                    parts.append(_extract_text_from_content(inner))
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def _convert_records_to_fa_json(records: List[Dict[str, Any]]) -> str:
    """Convert OpenClaw JSONL message records to FA-native PromptMessageExtended JSON.

    Filters records to ``type == "message"``, extracts user/assistant roles,
    converts content to plain text, and serialises via FastAgent's
    ``prompt_serialization.to_json``.

    Args:
        records (List[Dict[str, Any]]): Parsed JSONL records from one session file.

    Returns:
        str: FA-native JSON string suitable for writing to ``history.json``, or an
        empty string if there are no convertible messages.
    """
    from mcp.types import TextContent
    from fast_agent.mcp.prompt_serialization import to_json
    from fast_agent.types import PromptMessageExtended

    fa_messages: List[PromptMessageExtended] = []
    for rec in records:
        if rec.get("type") != "message":
            continue
        # OpenClaw v3: content is nested under rec["message"]
        msg = rec.get("message") or rec
        role = str(msg.get("role", "")).lower()
        if role not in ("user", "assistant"):
            continue
        raw_content = msg.get("content") or ""
        text = _extract_text_from_content(raw_content)
        if not text.strip():
            continue
        fa_messages.append(
            PromptMessageExtended(
                role=role,
                content=[TextContent(type="text", text=text)],
            )
        )
    if not fa_messages:
        return ""
    return to_json(fa_messages)


def _write_session_metadata(
    session_dir: Path,
    session_id: str,
    agent_name: str,
    records: List[Dict[str, Any]],
    created_at: Optional[datetime],
) -> None:
    """Write the pyclawops session.json metadata file for an imported session.

    Counts user/assistant messages from the record list, builds the pyclawops
    session metadata dict, and writes it atomically to
    ``session_dir/session.json`` (write-to-temp then rename).

    Args:
        session_dir (Path): Directory in which to create ``session.json``.
        session_id (str): pyclawops session ID (e.g. ``2024-01-15-aB3xY7``).
        agent_name (str): Name of the agent this session belongs to.
        records (List[Dict[str, Any]]): OpenClaw JSONL records used to count messages.
        created_at (Optional[datetime]): Session creation datetime; uses current time
            if None.

    Returns:
        None
    """
    now_iso = now().isoformat()
    created_iso = created_at.isoformat() if created_at else now_iso

    # Count messages (OpenClaw v3 nests role under rec["message"]["role"])
    msg_count = sum(
        1 for r in records
        if r.get("type") == "message"
        and (r.get("message") or r).get("role") in ("user", "assistant")
    )

    meta = {
        "id": session_id,
        "agent_id": agent_name,
        "channel": "openclaw",
        "user_id": "imported",
        "created_at": created_iso,
        "updated_at": created_iso,
        "message_count": msg_count,
        "is_active": False,
        "metadata": {"imported_from": "openclaw"},
        "context": {},
    }

    path = session_dir / "session.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(path)


def import_agent_sessions(
    agent_name: str,
    openclaw_dir: Path,
    pyclawops_dir: Path,
    verbose: bool = True,
) -> int:
    """Import all JSONL session files for a single agent from OpenClaw into pyclawops.

    Reads every non-deleted ``.jsonl`` file from
    ``openclaw_dir/agents/{agent_name}/sessions/``, converts messages to the
    FA-native JSON format, and writes ``history.json`` + ``session.json`` into
    ``pyclawops_dir/agents/{agent_name}/sessions/{session_id}/``.

    Args:
        agent_name (str): Agent name to import (matches the directory name).
        openclaw_dir (Path): Root OpenClaw data directory (e.g. ``~/.openclaw``).
        pyclawops_dir (Path): Root pyclawops data directory (e.g. ``~/.pyclawops``).
        verbose (bool): If True, prints progress and error messages to stdout.
            Defaults to True.

    Returns:
        int: Number of sessions successfully imported.
    """
    sessions_src = openclaw_dir / "agents" / agent_name / "sessions"
    if not sessions_src.exists():
        if verbose:
            print(f"  [skip] No OpenClaw sessions found at {sessions_src}")
        return 0

    sessions_dst_root = pyclawops_dir / "agents" / agent_name / "sessions"
    imported = 0

    for jsonl_file in sorted(f for f in sessions_src.glob("*.jsonl") if ".deleted" not in f.name):
        try:
            records = _load_jsonl(jsonl_file)
            if not records:
                continue

            fa_json = _convert_records_to_fa_json(records)
            if not fa_json:
                if verbose:
                    print(f"  [skip] {jsonl_file.name}: no convertible messages")
                continue

            dt = _openclaw_session_date(records)
            session_id = _gen_session_id(dt)
            session_dir = sessions_dst_root / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            # Write history
            hist_path = session_dir / "history.json"
            hist_path.write_text(fa_json)

            # Write metadata
            _write_session_metadata(session_dir, session_id, agent_name, records, dt)

            imported += 1
            if verbose:
                print(f"  Imported {jsonl_file.name} → {session_id}")

        except Exception as exc:
            if verbose:
                print(f"  [error] {jsonl_file.name}: {exc}")

    return imported


def cmd_import_openclaw(args: Any) -> None:
    """Entry point for the ``pyclawops import-openclaw`` CLI command.

    Reads ``args.openclaw_dir``, ``args.pyclawops_dir``, ``args.agent``, and
    ``args.all`` to determine which agents to import.  Calls
    ``import_agent_sessions`` for each agent and prints a summary.

    Args:
        args (Any): Parsed CLI arguments object with attributes:
            - ``openclaw_dir`` (Optional[str]): Path to OpenClaw data dir.
            - ``pyclawops_dir`` (Optional[str]): Path to pyclawops data dir.
            - ``agent`` (Optional[str]): Single agent name to import.
            - ``all`` (bool): If True, import all discovered agents.

    Returns:
        None

    Raises:
        SystemExit: If the OpenClaw directory is not found or neither ``--agent``
            nor ``--all`` is specified.
    """
    openclaw_dir = Path(getattr(args, "openclaw_dir", None) or _DEFAULT_OPENCLAW_DIR).expanduser()
    pyclawops_dir = Path(getattr(args, "pyclawops_dir", None) or _DEFAULT_PYCLAW_DIR).expanduser()
    agent_name: Optional[str] = getattr(args, "agent", None)
    import_all: bool = getattr(args, "all", False)

    if not openclaw_dir.exists():
        print(f"OpenClaw directory not found: {openclaw_dir}")
        import sys; sys.exit(1)

    # Discover agents
    openclaw_agents_dir = openclaw_dir / "agents"
    if not openclaw_agents_dir.exists():
        print(f"No agents directory found at {openclaw_agents_dir}")
        import sys; sys.exit(1)

    if agent_name:
        agents_to_import = [agent_name]
    elif import_all:
        agents_to_import = [
            d.name for d in openclaw_agents_dir.iterdir() if d.is_dir()
        ]
    else:
        print(
            "Specify --agent NAME or --all to import sessions.\n"
            "Example: pyclawops import-openclaw --all"
        )
        import sys; sys.exit(1)

    total = 0
    for name in agents_to_import:
        print(f"Importing sessions for agent: {name}")
        count = import_agent_sessions(name, openclaw_dir, pyclawops_dir)
        print(f"  → {count} session(s) imported")
        total += count

    print(f"\n✓ Done. {total} session(s) imported to {pyclawops_dir}/agents/")
    if total > 0:
        print("  Run 'pyclawops run' and the sessions will be available in the TUI session list.")
    print()
    print("⚠  Post-migration checklist:")
    print("   Review your agent markdown files for leftover OpenClaw paths:")
    print("   - PULSE.md  — update any script paths from ~/.openclaw/ to ~/.pyclawops/agents/{name}/")
    print("   - TOOLS.md  — update any tool/skill paths")
    print("   - scripts/  — update any hardcoded paths inside shell scripts or Python files")
    print("   Replace AGENTS.md with the pyclawops template (it still says 'OpenClaw' otherwise).")
