"""
FileMemoryBackend — per-agent markdown file memory store.

Each agent gets its own directory under ``~/.pyclaw/agents/{agent_name}/``:

    ~/.pyclaw/agents/myagent/
        MEMORY.md          # curated notes; user-edited; injected into sessions
                           # by prompt_builder.py (BOOTSTRAP_FILES)
        memory/
            2026-03-10.md  # daily journal written by memory tools
            2026-03-09.md
            vectors.json   # optional vector index (key → embedding)
            ...

``MEMORY.md`` is the curated file. It is NOT written by :meth:`write` —
the user (or agent) edits it manually.  Its content is available via
:meth:`read_curated` and is already injected into each session by
``pyclaw.core.prompt_builder.build_system_prompt``.

The ``memory/`` subdirectory holds append-friendly daily journals.
Each daily file uses this section format::

    # Memory — 2026-03-10

    ## key-name

    Content of the entry.

    Tags: tag1, tag2

    ---

    ## another-key

    More content here.

    ---

Vector index
------------
When an :class:`~pyclaw.memory.embeddings.EmbeddingBackend` is supplied, each
:meth:`write` call also stores the embedding in ``memory/vectors.json`` (a
JSON object mapping key → list[float]).  :meth:`search` then ranks results by
cosine similarity instead of keyword frequency.  Keys written before embeddings
were enabled score 0 and appear last.
"""

import json
import logging
import re
from datetime import datetime
from pyclaw.utils.time import now
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .backend import MemoryBackend
from .embeddings import cosine_similarity

if TYPE_CHECKING:
    from .embeddings import EmbeddingBackend

logger = logging.getLogger("pyclaw.memory")

_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_TAGS_RE = re.compile(r"^Tags:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


class FileMemoryBackend(MemoryBackend):
    """
    Per-agent file-based memory backend using markdown daily journals.

    Parameters
    ----------
    base_dir:
        The agent's root directory (e.g. ``~/.pyclaw/agents/myagent``).
        ``MEMORY.md`` lives here; daily journal files live in ``memory/``.
    embedding_backend:
        Optional :class:`~pyclaw.memory.embeddings.EmbeddingBackend`.  When
        provided, :meth:`write` indexes the entry and :meth:`search` ranks
        results by cosine similarity instead of keyword frequency.
    """

    def __init__(
        self,
        base_dir: str,
        embedding_backend: Optional["EmbeddingBackend"] = None,
    ) -> None:
        self._base = Path(base_dir).expanduser()
        self._daily_dir = self._base / "memory"
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        self._embedding_backend = embedding_backend

    @property
    def _vector_index_path(self) -> Path:
        return self._daily_dir / "vectors.json"

    def _load_vectors(self) -> Dict[str, List[float]]:
        """Load the vector index from disk; return empty dict if absent."""
        if not self._vector_index_path.exists():
            return {}
        try:
            return json.loads(self._vector_index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load vector index: %s", exc)
            return {}

    def _save_vectors(self, index: Dict[str, List[float]]) -> None:
        """Atomically write the vector index to disk."""
        try:
            tmp = self._vector_index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(index), encoding="utf-8")
            tmp.replace(self._vector_index_path)
        except Exception as exc:
            logger.warning("Could not save vector index: %s", exc)

    # ------------------------------------------------------------------ #
    # Public helpers (not part of abstract interface)
    # ------------------------------------------------------------------ #

    @property
    def curated_path(self) -> Path:
        """Path to the per-agent MEMORY.md file."""
        return self._base / "MEMORY.md"

    def read_curated(self) -> Optional[str]:
        """Return the contents of MEMORY.md, or None if it doesn't exist."""
        if self.curated_path.exists():
            return self.curated_path.read_text(encoding="utf-8")
        return None

    # ------------------------------------------------------------------ #
    # MemoryBackend interface
    # ------------------------------------------------------------------ #

    async def read(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the most recent entry for *key* (newest daily file first)."""
        for path in self._daily_files():
            entries = self._parse_daily(path)
            if key in entries:
                entry = entries[key]
                return {
                    "key": key,
                    "content": entry["content"],
                    "tags": entry["tags"],
                    "date": path.stem,
                }
        return None

    async def write(self, key: str, value: Dict[str, Any]) -> bool:
        """Create or update *key* in today's daily file.

        If an embedding backend is configured, the entry content is also
        embedded and stored in the vector index.
        """
        content = value.get("content", "")
        tags = value.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        self._upsert(self._today_path(), key, content, tags)

        if self._embedding_backend is not None and content:
            try:
                vectors = await self._embedding_backend.embed([content])
                index = self._load_vectors()
                index[key] = vectors[0]
                self._save_vectors(index)
            except Exception as exc:
                logger.warning("Could not embed key '%s': %s", key, exc)

        return True

    async def delete(self, key: str) -> bool:
        """Remove *key* from its most recent daily file and the vector index."""
        found = False
        for path in self._daily_files():
            entries = self._parse_daily(path)
            if key in entries:
                self._remove_section(path, key)
                logger.debug("Deleted memory key '%s' from %s", key, path.name)
                found = True
                break
        if not found:
            logger.debug("Delete: key '%s' not found", key)
            return False

        # Remove from vector index if present
        index = self._load_vectors()
        if key in index:
            del index[key]
            self._save_vectors(index)

        return True

    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """
        Search across MEMORY.md and all daily files.

        When an embedding backend is configured the query is embedded and
        results are ranked by cosine similarity.  Entries that have not yet
        been indexed (written before embeddings were enabled) receive a score
        of 0 and appear last.

        Falls back to keyword frequency scoring when no embedding backend is
        set.
        """
        if self._embedding_backend is not None:
            return await self._vector_search(query, limit)
        return await self._keyword_search(query, limit)

    async def _vector_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Embedding-based search via cosine similarity."""
        try:
            query_vecs = await self._embedding_backend.embed([query])  # type: ignore[union-attr]
            query_vec = query_vecs[0]
        except Exception as exc:
            logger.warning("Embedding query failed, falling back to keyword: %s", exc)
            return await self._keyword_search(query, limit)

        index = self._load_vectors()

        # Collect all entries with their date source
        all_entries: Dict[str, Dict[str, Any]] = {}
        if self.curated_path.exists():
            for key, entry in self._parse_daily(self.curated_path).items():
                all_entries[key] = {**entry, "date": "MEMORY.md"}
        for path in self._daily_files():
            for key, entry in self._parse_daily(path).items():
                if key not in all_entries:  # newest file wins
                    all_entries[key] = {**entry, "date": path.stem}

        candidates: List[Dict[str, Any]] = []
        for key, entry in all_entries.items():
            if key in index:
                score = cosine_similarity(query_vec, index[key])
            else:
                score = 0.0
            candidates.append({
                "key": key,
                "content": entry["content"],
                "tags": entry["tags"],
                "date": entry["date"],
                "score": score,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        for c in candidates:
            c.pop("score", None)
        return candidates[:limit]

    async def _keyword_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Keyword frequency search (original implementation)."""
        tokens = [t.lower() for t in query.split() if t]
        if not tokens:
            return []

        candidates: List[Dict[str, Any]] = []

        if self.curated_path.exists():
            for key, entry in self._parse_daily(self.curated_path).items():
                score = self._score(tokens, key + " " + entry["content"])
                if score > 0:
                    candidates.append({
                        "key": key,
                        "content": entry["content"],
                        "tags": entry["tags"],
                        "date": "MEMORY.md",
                        "score": score,
                    })

        for path in self._daily_files():
            for key, entry in self._parse_daily(path).items():
                score = self._score(tokens, key + " " + entry["content"])
                if score > 0:
                    candidates.append({
                        "key": key,
                        "content": entry["content"],
                        "tags": entry["tags"],
                        "date": path.stem,
                        "score": score,
                    })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        for c in candidates:
            c.pop("score", None)
        return candidates[:limit]

    async def reindex(self, batch_size: int = 32) -> Dict[str, Any]:
        """
        Re-embed all entries across all daily files and rebuild vectors.json.

        Useful after enabling embeddings on an existing memory directory, or
        after switching embedding models.  Entries are sent to the embedding
        backend in batches of *batch_size* to avoid rate-limit issues.

        Returns a summary dict::

            {"indexed": 12, "skipped": 0, "errors": 2}

        If no embedding backend is configured, returns immediately with all
        counts at zero.
        """
        if self._embedding_backend is None:
            return {"indexed": 0, "skipped": 0, "errors": 0}

        # Collect all unique keys with their content (newest file wins)
        all_entries: Dict[str, str] = {}
        for path in self._daily_files():
            for key, entry in self._parse_daily(path).items():
                if key not in all_entries:
                    all_entries[key] = entry["content"]

        if not all_entries:
            return {"indexed": 0, "skipped": 0, "errors": 0}

        index = self._load_vectors()
        keys = list(all_entries.keys())
        indexed = 0
        errors = 0

        for i in range(0, len(keys), batch_size):
            batch_keys = keys[i : i + batch_size]
            batch_texts = [all_entries[k] for k in batch_keys]
            try:
                vectors = await self._embedding_backend.embed(batch_texts)
                for key, vec in zip(batch_keys, vectors):
                    index[key] = vec
                    indexed += 1
            except Exception as exc:
                logger.warning("Reindex batch %d failed: %s", i // batch_size, exc)
                errors += len(batch_keys)

        self._save_vectors(index)
        logger.info(
            "Memory reindex complete: indexed=%d errors=%d dir=%s",
            indexed, errors, self._daily_dir,
        )
        return {"indexed": indexed, "skipped": 0, "errors": errors}

    async def list(self, prefix: str = "") -> List[str]:
        """List all keys across all daily files (deduplicated, newest wins)."""
        seen: dict[str, None] = {}
        for path in self._daily_files():
            for key in self._parse_daily(path):
                if not prefix or key.startswith(prefix):
                    seen.setdefault(key, None)
        return list(seen)

    # ------------------------------------------------------------------ #
    # File parsing
    # ------------------------------------------------------------------ #

    def _parse_daily(self, path: Path) -> Dict[str, Dict[str, Any]]:
        """Parse a daily (or MEMORY.md) file into ``{key: {content, tags}}``."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}

        entries: Dict[str, Dict[str, Any]] = {}
        parts = _HEADING_RE.split(text)
        for i in range(1, len(parts), 2):
            key = parts[i].strip()
            body = parts[i + 1] if i + 1 < len(parts) else ""
            body = re.sub(r"\n---\s*$", "", body.rstrip()).strip()
            tag_match = _TAGS_RE.search(body)
            tags: List[str] = []
            if tag_match:
                tags = [t.strip() for t in tag_match.group(1).split(",") if t.strip()]
                body = _TAGS_RE.sub("", body).strip()
            entries[key] = {"content": body, "tags": tags}
        return entries

    # ------------------------------------------------------------------ #
    # File mutation
    # ------------------------------------------------------------------ #

    def _upsert(self, path: Path, key: str, content: str, tags: List[str]) -> None:
        """Create or update a section in *path*."""
        if not path.exists():
            path.write_text(self._file_header(path.stem), encoding="utf-8")

        text = path.read_text(encoding="utf-8")
        section = self._format_section(key, content, tags)

        pattern = re.compile(
            rf"(^## {re.escape(key)}\n)(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        if pattern.search(text):
            new_text = pattern.sub(section, text)
        else:
            new_text = text.rstrip("\n") + "\n\n" + section

        path.write_text(new_text, encoding="utf-8")

    def _remove_section(self, path: Path, key: str) -> None:
        """Remove the section for *key* from *path*."""
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"^## {re.escape(key)}\n.*?(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        new_text = pattern.sub("", text).strip("\n") + "\n"
        path.write_text(new_text, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _today_path(self) -> Path:
        return self._daily_dir / f"{now().strftime('%Y-%m-%d')}.md"

    def _daily_files(self) -> List[Path]:
        """Return daily ``.md`` files sorted newest-first."""
        return sorted(self._daily_dir.glob("????-??-??.md"), reverse=True)

    @staticmethod
    def _file_header(stem: str) -> str:
        return f"# Memory — {stem}\n\n"

    @staticmethod
    def _format_section(key: str, content: str, tags: List[str]) -> str:
        body = content.strip()
        if tags:
            body += f"\n\nTags: {', '.join(tags)}"
        return f"## {key}\n\n{body}\n\n---\n\n"

    @staticmethod
    def _score(tokens: List[str], text: str) -> int:
        """Count how many query tokens appear in *text* (case-insensitive)."""
        lower = text.lower()
        return sum(lower.count(t) for t in tokens)
