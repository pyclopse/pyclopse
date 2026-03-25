"""DocLoader — reads pyclawops's self-knowledge base and source code.

Knowledge base lives at pyclawops/self/knowledge/ and is shipped as package data.
Source files are read from the pyclawops package directory (works in both dev and
installed-via-uv-tool environments because paths are resolved relative to this
file, not the working directory).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("pyclawops.self")

# pyclawops/self/knowledge/
_KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# pyclawops/ package root — one level up from pyclawops/self/
_PACKAGE_DIR = Path(__file__).parent.parent


class DocLoader:
    """Loads documentation topics and pyclawops source files.

    All paths are resolved at construction time so the loader is safe to reuse
    across many requests without repeated filesystem discovery.
    """

    def __init__(self) -> None:
        self._knowledge_dir = _KNOWLEDGE_DIR
        self._package_dir = _PACKAGE_DIR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def topics(self) -> str:
        """Return the full topic index as a formatted string.

        Reads knowledge/index.md and returns its content. If index.md is
        missing, falls back to walking the knowledge directory and listing
        every .md file found.
        """
        index_path = self._knowledge_dir / "index.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return self._build_topic_index()

    def read(self, topic: str) -> str:
        """Read documentation for *topic* (e.g. ``'architecture/gateway'``).

        *topic* is a path relative to knowledge/ without the ``.md`` extension.
        Returns the file contents or a helpful error message if not found.
        """
        topic = topic.strip().lstrip("/")

        # Try exact match, then with .md appended
        candidates = [
            self._knowledge_dir / topic,
            self._knowledge_dir / f"{topic}.md",
        ]
        for path in candidates:
            resolved = path.resolve()
            if not str(resolved).startswith(str(self._knowledge_dir.resolve())):
                return "[ERROR] Topic path escapes the knowledge directory."
            if resolved.exists() and resolved.is_file():
                return resolved.read_text(encoding="utf-8")

        # Suggest close matches
        available = self._list_topics()
        suggestions = [t for t in available if topic.split("/")[-1] in t]
        msg = f"[NOT FOUND] Topic '{topic}' not found."
        if suggestions:
            msg += f"\n\nDid you mean one of:\n" + "\n".join(f"  {s}" for s in suggestions[:5])
        else:
            msg += "\n\nUse self_topics() to see all available topics."
        return msg

    def source(self, module: str) -> str:
        """Read a pyclawops source file with line numbers.

        *module* is a path relative to the pyclawops package root, e.g.
        ``'core/gateway.py'`` or ``'agents/runner.py'``. Only paths within
        the pyclawops package are accessible; directory traversal is rejected.

        Returns the file contents with ``lineno\\t`` prefixes, matching the
        format used by the Read tool — familiar to agents trained on it.
        """
        module = module.strip().lstrip("/")

        resolved = (self._package_dir / module).resolve()
        package_root = self._package_dir.resolve()

        if not str(resolved).startswith(str(package_root)):
            return "[ERROR] Path escapes the pyclawops package directory."

        if not resolved.exists():
            return (
                f"[NOT FOUND] '{module}' not found in pyclawops package.\n"
                f"Package root: {package_root}\n"
                "Check the path — use forward slashes, no leading slash.\n"
                "Example: self_source('core/gateway.py')"
            )

        if not resolved.is_file():
            # It's a directory — list its contents helpfully
            entries = sorted(resolved.iterdir())
            lines = [f"'{module}' is a directory. Contents:"]
            for e in entries:
                suffix = "/" if e.is_dir() else ""
                lines.append(f"  {e.name}{suffix}")
            return "\n".join(lines)

        raw = resolved.read_text(encoding="utf-8")
        lines = raw.splitlines()
        width = len(str(len(lines)))
        numbered = "\n".join(
            f"{str(i + 1).rjust(width)}\t{line}" for i, line in enumerate(lines)
        )
        return f"# {module}\n\n{numbered}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_topics(self) -> list[str]:
        """Return a sorted list of all topic paths (without .md extension)."""
        if not self._knowledge_dir.exists():
            return []
        topics = []
        for path in sorted(self._knowledge_dir.rglob("*.md")):
            if path.name == "index.md":
                continue
            rel = path.relative_to(self._knowledge_dir)
            topic = str(rel).removesuffix(".md").replace("\\", "/")
            topics.append(topic)
        return topics

    def _build_topic_index(self) -> str:
        """Fallback index built by walking the knowledge directory."""
        topics = self._list_topics()
        if not topics:
            return "[EMPTY] No knowledge base topics found."
        lines = ["# pyclawops Self-Knowledge Topics", ""]
        for topic in topics:
            lines.append(f"  {topic}")
        lines.append("")
        lines.append("Use self_read('<topic>') to read any of these.")
        return "\n".join(lines)
