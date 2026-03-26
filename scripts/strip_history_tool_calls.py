#!/usr/bin/env python3
"""One-off migration: strip tool-call/result plumbing from all persisted history files.

Finds every history.json and history_previous.json under ~/.pyclopse/agents/,
applies the same _strip_tool_machinery logic used by _save_history(), and
writes the result back in-place.  Original files are backed up with a .bak
extension before being overwritten.

Usage:
    uv run python scripts/strip_history_tool_calls.py           # live run
    uv run python scripts/strip_history_tool_calls.py --dry-run  # preview only
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument(
        "--agents-dir",
        default="~/.pyclopse/agents",
        help="Root agents directory (default: ~/.pyclopse/agents)",
    )
    args = parser.parse_args()

    agents_dir = Path(args.agents_dir).expanduser()
    if not agents_dir.exists():
        print(f"Agents dir not found: {agents_dir}")
        sys.exit(1)

    # Import FA + pyclopse after confirming env is sane
    try:
        from fast_agent.mcp.prompt_serialization import load_messages, save_messages
    except ImportError as e:
        print(f"FastAgent not available: {e}")
        sys.exit(1)

    from pyclopse.agents.runner import _strip_tool_machinery, _trim_history_for_save

    history_files = sorted(agents_dir.glob("*/sessions/*/history*.json"))
    if not history_files:
        print("No history files found.")
        return

    total_files = len(history_files)
    skipped = 0
    unchanged = 0
    stripped = 0
    errors = 0
    total_msgs_before = 0
    total_msgs_after = 0
    total_bytes_before = 0
    total_bytes_after = 0

    for path in history_files:
        try:
            messages = load_messages(str(path))
        except Exception as e:
            print(f"  SKIP (load error): {path.relative_to(agents_dir)}  — {e}")
            errors += 1
            continue

        if not messages:
            skipped += 1
            continue

        cleaned = _strip_tool_machinery(list(messages))
        cleaned = _trim_history_for_save(cleaned)

        before_count = len(messages)
        after_count = len(cleaned)
        total_msgs_before += before_count
        total_msgs_after += after_count

        if after_count == before_count:
            # Check if content actually changed (tool_calls cleared on some messages)
            # Simple heuristic: re-serialise both and compare sizes
            import json
            from fast_agent.mcp.prompt_serialization import to_json
            orig_json = to_json(messages)
            new_json = to_json(cleaned)
            if orig_json == new_json:
                unchanged += 1
                total_bytes_before += len(orig_json)
                total_bytes_after += len(new_json)
                continue

        bytes_before = path.stat().st_size
        total_bytes_before += bytes_before

        rel = path.relative_to(agents_dir)
        dropped = before_count - after_count
        if args.dry_run:
            print(f"  DRY-RUN: {rel}  {before_count} → {after_count} msgs  (-{dropped})")
            stripped += 1
            total_bytes_after += bytes_before  # estimate unchanged for dry-run
            continue

        # Back up original
        bak = path.with_suffix(".json.bak")
        path.rename(bak)

        # Write cleaned version atomically
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=path.parent,
                prefix=".history.tmp.",
                suffix=".json",
            )
            tmp_path = Path(tmp.name)
            tmp.close()
            save_messages(cleaned, str(tmp_path))
            os.replace(tmp_path, path)
            bytes_after = path.stat().st_size
            total_bytes_after += bytes_after
            print(
                f"  OK: {rel}  {before_count} → {after_count} msgs  "
                f"({bytes_before:,} → {bytes_after:,} bytes, -{dropped} msgs)"
            )
            stripped += 1
        except Exception as e:
            # Restore backup on failure
            if bak.exists():
                bak.rename(path)
            print(f"  ERROR writing {rel}: {e}")
            errors += 1

    print()
    print("=" * 60)
    print(f"Total files found:   {total_files}")
    print(f"  Stripped:          {stripped}")
    print(f"  Unchanged:         {unchanged}")
    print(f"  Skipped (empty):   {skipped}")
    print(f"  Errors:            {errors}")
    print(f"Messages before:     {total_msgs_before:,}")
    print(f"Messages after:      {total_msgs_after:,}")
    print(f"Messages removed:    {total_msgs_before - total_msgs_after:,}")
    if total_bytes_before:
        saved = total_bytes_before - total_bytes_after
        pct = saved / total_bytes_before * 100
        print(f"Bytes before:        {total_bytes_before:,}")
        print(f"Bytes after:         {total_bytes_after:,}")
        print(f"Space saved:         {saved:,} bytes ({pct:.1f}%)")
    if not args.dry_run and stripped:
        print()
        print(f"Backups written as *.json.bak — delete them once you're satisfied.")


if __name__ == "__main__":
    main()
