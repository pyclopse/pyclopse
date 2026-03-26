"""BulkIngestor — batch-ingest session history and memory files into the vault.

Two source types are supported:

Sessions
    FA-native ``history.json`` files under
    ``~/.pyclopse/agents/{name}/sessions/*/history.json``.  Each file is a JSON
    object ``{"messages": [...]}`` where every message has ``role`` and
    ``content`` (string or list-of-content-parts).

Memory files
    Markdown files under ``~/.pyclopse/agents/{name}/memory/`` (daily journal
    files written by the memory tools) and ``MEMORY.md`` (the curated notes
    file).  Each file is ingested as a document via
    :meth:`IngestionHandler.ingest_document`.

Progress is tracked by the cursor store, so the ingestor is safe to re-run —
it skips already-processed sessions and documents.  On rate-limit errors the
ingestor backs off and retries, so overall progress is never lost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ingestion import IngestionHandler

logger = logging.getLogger("pyclopse.vault.bulk")

# How many messages to feed the extraction agent per call.
_CHUNK_SIZE = 10

# Session channels that contain no user-facing content worth extracting
_SKIP_CHANNELS = {"job", "a2a", "openclaw"}

# Retry config for rate-limit errors
_RETRY_INITIAL_WAIT = 5.0   # seconds
_RETRY_MAX_WAIT = 120.0
_RETRY_BACKOFF = 2.0
_RETRY_MAX_ATTEMPTS = 6


@dataclass
class BulkIngestStats:
    """Running totals returned by BulkIngestor.run()."""

    sessions_processed: int = 0
    sessions_skipped: int = 0
    documents_processed: int = 0
    documents_skipped: int = 0
    facts_extracted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Sessions:  {self.sessions_processed} processed, {self.sessions_skipped} skipped",
            f"Documents: {self.documents_processed} processed, {self.documents_skipped} skipped",
            f"Facts extracted: {self.facts_extracted}",
        ]
        if self.errors:
            lines.append(f"Errors ({len(self.errors)}):")
            for e in self.errors[:5]:
                lines.append(f"  • {e}")
            if len(self.errors) > 5:
                lines.append(f"  … and {len(self.errors) - 5} more")
        return "\n".join(lines)


def _extract_text(content) -> str:
    """Flatten FA content (string or list of content parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(str(part))
        return " ".join(p for p in parts if p)
    return str(content) if content else ""


def _is_boring(messages: list[dict]) -> bool:
    """Return True if the message list has no substantive user/assistant exchange."""
    texts = [_extract_text(m.get("content", "")) for m in messages
             if m.get("role") in ("user", "assistant")]
    total = sum(len(t) for t in texts)
    return total < 50


async def _with_retry(coro_fn, description: str) -> tuple[any, str | None]:
    """Call coro_fn() with exponential back-off on rate-limit errors.

    Returns (result, None) on success or (None, error_str) after all retries
    are exhausted.
    """
    wait = _RETRY_INITIAL_WAIT
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            result = await coro_fn()
            return result, None
        except Exception as exc:
            msg = str(exc).lower()
            is_rate_limit = any(k in msg for k in ("rate limit", "rate_limit", "429", "too many requests"))
            if is_rate_limit and attempt < _RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "%s: rate-limited (attempt %d/%d), waiting %.0fs",
                    description, attempt, _RETRY_MAX_ATTEMPTS, wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * _RETRY_BACKOFF, _RETRY_MAX_WAIT)
            else:
                return None, f"{description}: {exc}"
    return None, f"{description}: max retries exhausted"


class BulkIngestor:
    """Batch-ingest session history and memory files into the vault.

    Args:
        agent_dir: Path to the agent's workspace directory
            (e.g. ``~/.pyclopse/agents/niggy``).
        ingestion_handler: Configured :class:`IngestionHandler` instance.
        channel: Channel name recorded against extracted facts (default ``"bulk"``).
        progress_callback: Optional async callable ``(message: str) -> None``
            called with progress updates as ingestion proceeds.
    """

    def __init__(
        self,
        agent_dir: Path,
        ingestion_handler: IngestionHandler,
        channel: str = "bulk",
        progress_callback=None,
    ) -> None:
        self._agent_dir = Path(agent_dir).expanduser()
        self._handler = ingestion_handler
        self._channel = channel
        self._progress = progress_callback

    async def _emit(self, msg: str) -> None:
        if self._progress:
            try:
                result = self._progress(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        logger.info(msg)

    async def run(
        self,
        include_sessions: bool = True,
        include_memory: bool = True,
    ) -> BulkIngestStats:
        """Run the bulk ingest and return stats.

        Args:
            include_sessions: Whether to ingest FA session history files.
            include_memory: Whether to ingest memory markdown files.

        Returns:
            :class:`BulkIngestStats` with totals and any errors.
        """
        stats = BulkIngestStats()

        if include_sessions:
            await self._ingest_sessions(stats)

        if include_memory:
            await self._ingest_memory_files(stats)

        await self._emit(f"Bulk ingest complete.\n{stats.summary()}")
        return stats

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    async def _ingest_sessions(self, stats: BulkIngestStats) -> None:
        sessions_dir = self._agent_dir / "sessions"
        if not sessions_dir.exists():
            return

        history_files = sorted(sessions_dir.glob("*/history.json"))
        await self._emit(f"Found {len(history_files)} session(s) to scan…")

        for history_file in history_files:
            session_id = history_file.parent.name
            await self._ingest_session_file(session_id, history_file, stats)

    async def _ingest_session_file(
        self,
        session_id: str,
        history_file: Path,
        stats: BulkIngestStats,
    ) -> None:
        import json

        try:
            raw = json.loads(history_file.read_text(encoding="utf-8"))
        except Exception as e:
            stats.errors.append(f"session {session_id}: failed to read history: {e}")
            return

        # Skip sessions from channels that contain no extractable user content
        session_json = history_file.parent / "session.json"
        if session_json.exists():
            try:
                import json as _json
                sess_data = _json.loads(session_json.read_text(encoding="utf-8"))
                if sess_data.get("channel", "") in _SKIP_CHANNELS:
                    stats.sessions_skipped += 1
                    return
            except Exception:
                pass

        all_messages = raw.get("messages", [])
        if not all_messages:
            stats.sessions_skipped += 1
            return

        # Flatten to simple dicts with plain-text content
        simple: list[dict] = []
        for m in all_messages:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _extract_text(m.get("content", ""))
            if text:
                simple.append({"role": role, "content": text})

        if not simple or _is_boring(simple):
            stats.sessions_skipped += 1
            return

        # Check cursor — find the starting index
        cursor_entry = self._handler._cursor.get_session_cursor(session_id)
        start_index = cursor_entry.last_message_index if cursor_entry else 0

        if start_index >= len(simple):
            stats.sessions_skipped += 1
            return

        await self._emit(
            f"Session {session_id}: ingesting messages {start_index}–{len(simple)}…"
        )

        # Process in chunks
        i = start_index
        while i < len(simple):
            chunk = simple[i: i + _CHUNK_SIZE]
            chunk_end = i + len(chunk)

            result, err = await _with_retry(
                lambda c=chunk, ci=i, ce=chunk_end: self._handler.ingest_conversation_turn(
                    session_id=session_id,
                    messages=c,
                    message_range=(ci, ce),
                    channel=self._channel,
                ),
                f"session {session_id} [{i}:{chunk_end}]",
            )

            if err:
                # Throttled by usage monitor — bail gracefully, cursor keeps progress
                if "throttled" in err.lower() or "usage" in err.lower():
                    await self._emit(f"  Throttled — pausing ingest for session {session_id}")
                    return
                stats.errors.append(err)
                return  # stop this session, cursor stays at last success

            if result and not result.skip_reason:
                stats.facts_extracted += len(result.extractions)

            i = chunk_end

        stats.sessions_processed += 1

    # ------------------------------------------------------------------ #
    # Memory files
    # ------------------------------------------------------------------ #

    async def _ingest_memory_files(self, stats: BulkIngestStats) -> None:
        candidates: list[Path] = []

        # MEMORY.md (curated notes)
        memory_md = self._agent_dir / "MEMORY.md"
        if memory_md.exists():
            candidates.append(memory_md)

        # Daily journal files under memory/
        memory_dir = self._agent_dir / "memory"
        if memory_dir.exists():
            candidates.extend(sorted(
                p for p in memory_dir.glob("*.md")
                if p.name != "vectors.json"
            ))

        await self._emit(f"Found {len(candidates)} memory file(s) to scan…")

        for path in candidates:
            await self._ingest_memory_file(path, stats)

    async def _ingest_memory_file(self, path: Path, stats: BulkIngestStats) -> None:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception as e:
            stats.errors.append(f"{path.name}: failed to read: {e}")
            return

        if not content or len(content) < 50:
            stats.documents_skipped += 1
            return

        result, err = await _with_retry(
            lambda p=str(path), c=content: self._handler.ingest_document(
                file_path=p,
                content=c,
            ),
            f"document {path.name}",
        )

        if err:
            stats.errors.append(err)
            return

        if result and result.skip_reason == "already_processed_same_hash":
            stats.documents_skipped += 1
            return

        if result and not result.skip_reason:
            stats.facts_extracted += len(result.extractions)

        stats.documents_processed += 1
        await self._emit(f"  {path.name}: done")
