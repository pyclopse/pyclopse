"""CursorStore — tracks vault processing progress for sessions and documents.

The cursor store persists to {vault_dir}/.cursors.json and is used to:
- Track which sessions have been processed (and up to which message index)
- Track which documents have been processed (and their content hash)
- Detect crash recovery via currently_processing
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import CursorStoreData, DocumentCursor, SessionCursor

logger = logging.getLogger("pyclopse.vault.cursor")

_CURSORS_FILE = ".cursors.json"
_JOB_CHANNELS = {"job", "a2a"}


class CursorStore:
    """Manages vault processing cursors for sessions and documents."""

    def __init__(self, vault_dir: Path) -> None:
        self._vault_dir = vault_dir
        self._cursors_path = vault_dir / _CURSORS_FILE
        vault_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Session cursors
    # ------------------------------------------------------------------

    def get_session_cursor(self, session_id: str) -> Optional[SessionCursor]:
        """Return the session cursor for session_id, or None if not tracked."""
        data = self._load()
        return data.sessions.get(session_id)

    def update_session_cursor(
        self,
        session_id: str,
        last_message_index: int,
        channel: str = "",
    ) -> None:
        """Update (or create) the session cursor for session_id."""
        data = self._load()
        existing = data.sessions.get(session_id)
        if existing is not None:
            data.sessions[session_id] = SessionCursor(
                session_id=session_id,
                last_message_index=last_message_index,
                last_processed_at=datetime.now(timezone.utc),
                channel=channel or existing.channel,
            )
        else:
            data.sessions[session_id] = SessionCursor(
                session_id=session_id,
                last_message_index=last_message_index,
                last_processed_at=datetime.now(timezone.utc),
                channel=channel,
            )
        self._save(data)

    def get_unprocessed_sessions(self, sessions_dir: Path) -> list[tuple[str, int]]:
        """Scan sessions_dir for sessions where cursor lags behind message count.

        Skips sessions with channel=job or channel=a2a.

        Returns:
            List of (session_id, messages_to_process) tuples, sorted oldest first.
        """
        if not sessions_dir.exists():
            return []

        data = self._load()
        results: list[tuple[str, int, float]] = []  # (session_id, to_process, mtime)

        for history_file in sessions_dir.glob("*/history.json"):
            session_dir = history_file.parent
            session_id = session_dir.name

            # Check channel from session.json if available
            session_json = session_dir / "session.json"
            channel = ""
            if session_json.exists():
                try:
                    sess_data = json.loads(session_json.read_text(encoding="utf-8"))
                    channel = str(sess_data.get("channel", ""))
                except Exception:
                    pass

            if channel in _JOB_CHANNELS:
                continue

            # Count messages in history.json
            try:
                history_data = json.loads(history_file.read_text(encoding="utf-8"))
                if isinstance(history_data, list):
                    message_count = len(history_data)
                else:
                    message_count = 0
            except Exception:
                continue

            if message_count == 0:
                continue

            cursor = data.sessions.get(session_id)
            cursor_index = cursor.last_message_index if cursor is not None else 0

            # Update channel in cursor if we now know it
            if cursor is not None and channel and cursor.channel != channel:
                cursor = SessionCursor(
                    session_id=session_id,
                    last_message_index=cursor.last_message_index,
                    last_processed_at=cursor.last_processed_at,
                    channel=channel,
                )
                data.sessions[session_id] = cursor

            to_process = message_count - cursor_index
            if to_process > 0:
                mtime = history_file.stat().st_mtime
                results.append((session_id, to_process, mtime))

        if results:
            self._save(data)

        # Sort oldest first (smallest mtime first)
        results.sort(key=lambda x: x[2])
        return [(sid, count) for sid, count, _ in results]

    # ------------------------------------------------------------------
    # Document cursors
    # ------------------------------------------------------------------

    def get_document_cursor(self, file_path: str) -> Optional[DocumentCursor]:
        """Return the document cursor for file_path, or None if not tracked."""
        data = self._load()
        return data.documents.get(file_path)

    def update_document_cursor(
        self,
        file_path: str,
        file_hash: str,
        fact_ids: list[str],
    ) -> None:
        """Update (or create) the document cursor for file_path."""
        data = self._load()
        data.documents[file_path] = DocumentCursor(
            file_path=file_path,
            last_hash=file_hash,
            last_processed_at=datetime.now(timezone.utc),
            extracted_fact_ids=list(fact_ids),
        )
        self._save(data)

    def get_extracted_fact_ids(self, file_path: str) -> list[str]:
        """Return fact IDs extracted from a document. Empty list if unknown."""
        cursor = self.get_document_cursor(file_path)
        if cursor is None:
            return []
        return list(cursor.extracted_fact_ids)

    def get_deleted_documents(self, memory_dir: Path) -> list[str]:
        """Return file_paths in cursor store that no longer exist on disk."""
        data = self._load()
        deleted = []
        for file_path in data.documents:
            if not Path(file_path).exists():
                deleted.append(file_path)
        return deleted

    # ------------------------------------------------------------------
    # Currently processing (crash recovery)
    # ------------------------------------------------------------------

    def get_currently_processing(self) -> Optional[dict[str, Any]]:
        """Return the currently_processing item, or None."""
        return self._load().currently_processing

    def set_currently_processing(self, item: dict[str, Any]) -> None:
        """Set the currently_processing item."""
        data = self._load()
        data.currently_processing = item
        self._save(data)

    def clear_currently_processing(self) -> None:
        """Clear the currently_processing item."""
        data = self._load()
        data.currently_processing = None
        self._save(data)

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> CursorStoreData:
        """Load cursor store from disk. Returns empty store if file absent."""
        if not self._cursors_path.exists():
            return CursorStoreData()
        try:
            raw = json.loads(self._cursors_path.read_text(encoding="utf-8"))
            return CursorStoreData.model_validate(raw)
        except Exception as exc:
            logger.warning("Failed to load cursor store, starting fresh: %s", exc)
            return CursorStoreData()

    def _save(self, data: CursorStoreData) -> None:
        """Atomically write cursor store to disk (tmp + rename)."""
        content = data.model_dump_json(indent=2)
        tmp = self._cursors_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, self._cursors_path)
