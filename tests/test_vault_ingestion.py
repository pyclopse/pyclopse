"""Tests for IngestionHandler."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclawops.memory.vault.agent import MockMemoryAgent
from pyclawops.memory.vault.cursor import CursorStore
from pyclawops.memory.vault.ingestion import IngestionHandler
from pyclawops.memory.vault.models import (
    ExtractionAction,
    ExtractionResult,
    FactExtraction,
    MemoryType,
    SourceSession,
    VaultFact,
    VaultFactState,
)
from pyclawops.memory.vault.registry import TypeSchemaRegistry
from pyclawops.memory.vault.search import FallbackSearchBackend
from pyclawops.memory.vault.store import VaultStore
from pyclawops.memory.vault.ulid import generate as gen_ulid


def make_handler(
    tmp_path: Path,
    agent_results: list[ExtractionResult] = None,
) -> tuple[IngestionHandler, VaultStore, CursorStore]:
    store = VaultStore(tmp_path)
    cursor = CursorStore(tmp_path)
    search = FallbackSearchBackend(store)
    registry = TypeSchemaRegistry()
    agent = MockMemoryAgent(results=agent_results or [])
    handler = IngestionHandler(
        vault_dir=tmp_path,
        store=store,
        cursor=cursor,
        search=search,
        registry=registry,
        agent=agent,
    )
    return handler, store, cursor


SAMPLE_MESSAGES = [
    {"role": "user", "content": "I prefer Python over Go"},
    {"role": "assistant", "content": "Noted, I'll use Python in my examples"},
]


class TestIngestConversation:
    async def test_ingest_conversation_creates_fact(self, tmp_path):
        fact_fields = {
            "type": "preference",
            "claim": "User prefers Python over Go",
            "confidence": 0.85,
        }
        agent_result = ExtractionResult(
            extractions=[FactExtraction(action=ExtractionAction.CREATE, fact_fields=fact_fields)]
        )
        handler, store, _ = make_handler(tmp_path, [agent_result])

        result = await handler.ingest_conversation_turn(
            session_id="sess-001",
            messages=SAMPLE_MESSAGES,
            message_range=(0, 2),
            channel="telegram",
        )

        assert len(result.extractions) == 1
        facts = store.list_facts()
        assert len(facts) == 1
        assert facts[0].claim == "User prefers Python over Go"
        assert facts[0].state == VaultFactState.PROVISIONAL

    async def test_ingest_conversation_reinforces_existing(self, tmp_path):
        # Write an existing fact first
        existing = VaultFact(
            id=gen_ulid(),
            type="preference",
            state=VaultFactState.PROVISIONAL,
            claim="User prefers Python",
            confidence=0.8,
        )
        store = VaultStore(tmp_path)
        store.write_fact(existing)

        agent_result = ExtractionResult(
            extractions=[
                FactExtraction(
                    action=ExtractionAction.REINFORCE,
                    fact_fields={},
                    target_id=existing.id,
                )
            ]
        )
        cursor = CursorStore(tmp_path)
        search = FallbackSearchBackend(store)
        registry = TypeSchemaRegistry()
        agent = MockMemoryAgent(results=[agent_result])
        handler = IngestionHandler(tmp_path, store, cursor, search, registry, agent)

        result = await handler.ingest_conversation_turn(
            session_id="sess-001",
            messages=SAMPLE_MESSAGES,
            message_range=(0, 2),
            channel="telegram",
        )

        updated = store.read_fact(existing.id)
        assert updated.reinforcement_count == 1

    async def test_ingest_conversation_supersedes_contradiction(self, tmp_path):
        # Write old fact
        old = VaultFact(
            id=gen_ulid(),
            type="preference",
            state=VaultFactState.PROVISIONAL,
            claim="User prefers Python",
            confidence=0.8,
        )
        store = VaultStore(tmp_path)
        store.write_fact(old)

        new_id = gen_ulid()
        agent_result = ExtractionResult(
            extractions=[
                FactExtraction(
                    action=ExtractionAction.SUPERSEDE,
                    fact_fields={
                        "id": new_id,
                        "type": "preference",
                        "claim": "User now prefers Rust over Python",
                        "confidence": 0.9,
                    },
                    supersedes_id=old.id,
                )
            ]
        )
        cursor = CursorStore(tmp_path)
        search = FallbackSearchBackend(store)
        registry = TypeSchemaRegistry()
        agent = MockMemoryAgent(results=[agent_result])
        handler = IngestionHandler(tmp_path, store, cursor, search, registry, agent)

        await handler.ingest_conversation_turn(
            session_id="sess-001",
            messages=SAMPLE_MESSAGES,
            message_range=(0, 2),
            channel="telegram",
        )

        old_loaded = store.read_fact(old.id)
        assert old_loaded.state == VaultFactState.SUPERSEDED
        assert old_loaded.superseded_by == new_id

        new_loaded = store.read_fact(new_id)
        assert new_loaded is not None
        assert new_loaded.supersedes == old.id

    async def test_ingest_conversation_skips_on_skip_reason(self, tmp_path):
        agent_result = ExtractionResult(
            extractions=[],
            skip_reason="Not enough content to extract",
        )
        handler, store, cursor = make_handler(tmp_path, [agent_result])

        result = await handler.ingest_conversation_turn(
            session_id="sess-skip",
            messages=SAMPLE_MESSAGES,
            message_range=(0, 2),
            channel="telegram",
        )

        assert result.skip_reason == "Not enough content to extract"
        assert store.list_facts() == []
        # Cursor should still advance
        c = cursor.get_session_cursor("sess-skip")
        assert c is not None
        assert c.last_message_index == 2

    async def test_cursor_updated_after_ingestion(self, tmp_path):
        agent_result = ExtractionResult(extractions=[])
        handler, _, cursor = make_handler(tmp_path, [agent_result])

        await handler.ingest_conversation_turn(
            session_id="sess-update",
            messages=SAMPLE_MESSAGES,
            message_range=(5, 10),
            channel="slack",
        )

        c = cursor.get_session_cursor("sess-update")
        assert c is not None
        assert c.last_message_index == 10
        assert c.channel == "slack"

    async def test_currently_processing_cleared_after_ingestion(self, tmp_path):
        agent_result = ExtractionResult(extractions=[])
        handler, _, cursor = make_handler(tmp_path, [agent_result])

        await handler.ingest_conversation_turn(
            session_id="sess-001",
            messages=SAMPLE_MESSAGES,
            message_range=(0, 2),
            channel="telegram",
        )

        assert cursor.get_currently_processing() is None


class TestIngestDocument:
    async def test_ingest_document_creates_facts(self, tmp_path):
        fact_fields = {
            "type": "fact",
            "claim": "Python was created by Guido van Rossum",
            "confidence": 0.99,
        }
        agent_result = ExtractionResult(
            extractions=[FactExtraction(action=ExtractionAction.CREATE, fact_fields=fact_fields)]
        )
        handler, store, _ = make_handler(tmp_path, [agent_result])

        result = await handler.ingest_document(
            file_path="/docs/python-history.md",
            content="# Python History\n\nPython was created by Guido van Rossum.",
        )

        assert len(result.extractions) == 1
        facts = store.list_facts(source_file="/docs/python-history.md")
        assert len(facts) == 1

    async def test_ingest_document_skips_same_hash(self, tmp_path):
        agent_result = ExtractionResult(
            extractions=[FactExtraction(
                action=ExtractionAction.CREATE,
                fact_fields={"type": "fact", "claim": "A fact"},
            )]
        )
        handler, store, cursor = make_handler(tmp_path, [agent_result, agent_result])

        content = "# Doc\n\nSome content."

        # First ingestion
        result1 = await handler.ingest_document("/docs/test.md", content)
        assert result1.skip_reason is None

        # Second ingestion with same content
        result2 = await handler.ingest_document("/docs/test.md", content)
        assert result2.skip_reason == "already_processed_same_hash"

        # Only one fact created
        assert len(store.list_facts()) == 1

    async def test_ingest_document_cursor_updated(self, tmp_path):
        import hashlib
        agent_result = ExtractionResult(extractions=[])
        handler, _, cursor = make_handler(tmp_path, [agent_result])
        content = "Some document content."

        await handler.ingest_document("/docs/test.md", content)

        doc_cursor = cursor.get_document_cursor("/docs/test.md")
        assert doc_cursor is not None
        expected_hash = hashlib.sha256(content.encode()).hexdigest()
        assert doc_cursor.last_hash == expected_hash


class TestHandleDocumentDeleted:
    async def test_handle_document_deleted(self, tmp_path):
        store = VaultStore(tmp_path)
        # Create facts with source_file
        f1 = VaultFact(
            id=gen_ulid(),
            type="fact",
            state=VaultFactState.PROVISIONAL,
            claim="Fact from doc",
            confidence=0.8,
            source_file="/docs/old.md",
        )
        store.write_fact(f1)

        cursor = CursorStore(tmp_path)
        cursor.update_document_cursor("/docs/old.md", "hash123", [f1.id])

        search = FallbackSearchBackend(store)
        registry = TypeSchemaRegistry()
        agent = MockMemoryAgent()
        handler = IngestionHandler(tmp_path, store, cursor, search, registry, agent)

        archived = await handler.handle_document_deleted("/docs/old.md")

        assert f1.id in archived
        loaded = store.read_fact(f1.id)
        assert loaded.state == VaultFactState.ARCHIVED


class TestRunCatchUp:
    async def test_run_catch_up_processes_unprocessed(self, tmp_path):
        sessions_dir = tmp_path / "sessions"

        # Create a session with messages
        sess_dir = sessions_dir / "sess-001"
        sess_dir.mkdir(parents=True)
        messages = [{"role": "user", "content": "Hello"} for _ in range(5)]
        (sess_dir / "history.json").write_text(json.dumps(messages))
        (sess_dir / "session.json").write_text(
            json.dumps({"channel": "telegram", "id": "sess-001"})
        )

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        agent_result = ExtractionResult(
            extractions=[
                FactExtraction(
                    action=ExtractionAction.CREATE,
                    fact_fields={"type": "fact", "claim": "Test fact", "confidence": 0.7},
                )
            ]
        )
        handler, store, _ = make_handler(tmp_path, [agent_result])

        stats = await handler.run_catch_up(sessions_dir, memory_dir)

        assert stats["sessions_processed"] == 1
        assert stats["errors"] == 0

    async def test_crash_recovery_resets_processing(self, tmp_path):
        """If currently_processing is set from a crash, it should be cleared on next run."""
        handler, _, cursor = make_handler(tmp_path)

        # Simulate a crash-left state
        cursor.set_currently_processing({"type": "conversation", "session_id": "crashed-sess"})
        assert cursor.get_currently_processing() is not None

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        stats = await handler.run_catch_up(sessions_dir, memory_dir)

        # Should have cleared the stuck state
        assert cursor.get_currently_processing() is None
