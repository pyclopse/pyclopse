"""Tests for CursorStore."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pyclaw.memory.vault.cursor import CursorStore


def make_session_dir(base: Path, session_id: str, messages: list, channel: str = "telegram") -> Path:
    """Create a mock session directory with history.json and session.json."""
    sess_dir = base / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "history.json").write_text(json.dumps(messages), encoding="utf-8")
    (sess_dir / "session.json").write_text(
        json.dumps({"channel": channel, "id": session_id}),
        encoding="utf-8",
    )
    return sess_dir


class TestEmptyCursorStore:
    def test_empty_returns_none_for_session(self, tmp_path):
        store = CursorStore(tmp_path)
        assert store.get_session_cursor("nonexistent") is None

    def test_empty_returns_none_for_document(self, tmp_path):
        store = CursorStore(tmp_path)
        assert store.get_document_cursor("/some/file.md") is None

    def test_empty_extracted_fact_ids(self, tmp_path):
        store = CursorStore(tmp_path)
        assert store.get_extracted_fact_ids("/file.md") == []

    def test_empty_currently_processing(self, tmp_path):
        store = CursorStore(tmp_path)
        assert store.get_currently_processing() is None


class TestSessionCursor:
    def test_session_cursor_update(self, tmp_path):
        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-001", last_message_index=5, channel="telegram")
        cursor = store.get_session_cursor("sess-001")
        assert cursor is not None
        assert cursor.session_id == "sess-001"
        assert cursor.last_message_index == 5
        assert cursor.channel == "telegram"

    def test_session_cursor_persists_across_reload(self, tmp_path):
        store1 = CursorStore(tmp_path)
        store1.update_session_cursor("sess-001", 10, "slack")

        store2 = CursorStore(tmp_path)
        cursor = store2.get_session_cursor("sess-001")
        assert cursor is not None
        assert cursor.last_message_index == 10

    def test_session_cursor_update_overwrites(self, tmp_path):
        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-001", 5, "telegram")
        store.update_session_cursor("sess-001", 15, "telegram")
        cursor = store.get_session_cursor("sess-001")
        assert cursor.last_message_index == 15

    def test_session_cursor_recovery(self, tmp_path):
        """currently_processing should be resettable (crash recovery)."""
        store = CursorStore(tmp_path)
        store.set_currently_processing({"type": "conversation", "session_id": "sess-crash"})
        assert store.get_currently_processing() is not None

        store.clear_currently_processing()
        assert store.get_currently_processing() is None


class TestDocumentCursor:
    def test_document_cursor_update(self, tmp_path):
        store = CursorStore(tmp_path)
        store.update_document_cursor("/docs/notes.md", "abc123", ["ID1", "ID2"])
        cursor = store.get_document_cursor("/docs/notes.md")
        assert cursor is not None
        assert cursor.last_hash == "abc123"
        assert cursor.extracted_fact_ids == ["ID1", "ID2"]

    def test_document_cursor_persists(self, tmp_path):
        store1 = CursorStore(tmp_path)
        store1.update_document_cursor("/docs/x.md", "hash1", ["FACT-A"])

        store2 = CursorStore(tmp_path)
        cursor = store2.get_document_cursor("/docs/x.md")
        assert cursor is not None
        assert cursor.last_hash == "hash1"

    def test_get_extracted_fact_ids(self, tmp_path):
        store = CursorStore(tmp_path)
        store.update_document_cursor("/docs/test.md", "hash", ["F1", "F2", "F3"])
        ids = store.get_extracted_fact_ids("/docs/test.md")
        assert ids == ["F1", "F2", "F3"]

    def test_get_extracted_fact_ids_unknown(self, tmp_path):
        store = CursorStore(tmp_path)
        assert store.get_extracted_fact_ids("/nonexistent.md") == []


class TestDeletedDocuments:
    def test_get_deleted_documents(self, tmp_path):
        """Files tracked in cursor but no longer on disk should be returned."""
        store = CursorStore(tmp_path)

        # Create a real file and track it
        real_file = tmp_path / "real.md"
        real_file.write_text("content")
        store.update_document_cursor(str(real_file), "hash1", [])

        # Track a non-existent file
        fake_path = str(tmp_path / "deleted.md")
        store.update_document_cursor(fake_path, "hash2", [])

        deleted = store.get_deleted_documents(tmp_path)
        assert fake_path in deleted
        assert str(real_file) not in deleted


class TestUnprocessedSessions:
    def test_get_unprocessed_sessions(self, tmp_path):
        """Sessions with more messages than cursor should be returned."""
        sessions_dir = tmp_path / "sessions"

        # Session with 5 messages, cursor at 0
        make_session_dir(sessions_dir, "sess-001", [{"role": "user", "content": f"msg {i}"} for i in range(5)])

        store = CursorStore(tmp_path)
        unprocessed = store.get_unprocessed_sessions(sessions_dir)
        assert len(unprocessed) == 1
        assert unprocessed[0][0] == "sess-001"
        assert unprocessed[0][1] == 5  # 5 messages to process

    def test_processed_session_excluded(self, tmp_path):
        """Sessions already processed should not appear."""
        sessions_dir = tmp_path / "sessions"
        make_session_dir(sessions_dir, "sess-done", [{"role": "user"} for _ in range(3)])

        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-done", 3, "telegram")

        unprocessed = store.get_unprocessed_sessions(sessions_dir)
        assert unprocessed == []

    def test_job_channel_skipped(self, tmp_path):
        """Sessions with channel=job should be skipped."""
        sessions_dir = tmp_path / "sessions"
        make_session_dir(sessions_dir, "job-sess", [{"role": "user"} for _ in range(5)], channel="job")

        store = CursorStore(tmp_path)
        unprocessed = store.get_unprocessed_sessions(sessions_dir)
        assert unprocessed == []

    def test_a2a_channel_skipped(self, tmp_path):
        """Sessions with channel=a2a should be skipped."""
        sessions_dir = tmp_path / "sessions"
        make_session_dir(sessions_dir, "a2a-sess", [{"role": "user"} for _ in range(3)], channel="a2a")

        store = CursorStore(tmp_path)
        unprocessed = store.get_unprocessed_sessions(sessions_dir)
        assert unprocessed == []

    def test_partial_processing(self, tmp_path):
        """Only unprocessed messages should be counted."""
        sessions_dir = tmp_path / "sessions"
        make_session_dir(sessions_dir, "sess-partial", [{"role": "user"} for _ in range(10)])

        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-partial", 7, "telegram")

        unprocessed = store.get_unprocessed_sessions(sessions_dir)
        assert len(unprocessed) == 1
        assert unprocessed[0][1] == 3  # 10 - 7 = 3 remaining

    def test_no_sessions_dir(self, tmp_path):
        store = CursorStore(tmp_path)
        unprocessed = store.get_unprocessed_sessions(tmp_path / "nonexistent")
        assert unprocessed == []


class TestAtomicSave:
    def test_atomic_save_no_tmp_files(self, tmp_path):
        """No .tmp files should remain after save."""
        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-001", 5, "telegram")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_cursors_json_created(self, tmp_path):
        store = CursorStore(tmp_path)
        store.update_session_cursor("sess-001", 1, "slack")
        assert (tmp_path / ".cursors.json").exists()
