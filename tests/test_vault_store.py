"""Tests for VaultStore."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from pyclopse.memory.vault.models import (
    SourceSession,
    VaultFact,
    VaultFactState,
)
from pyclopse.memory.vault.store import VaultStore
from pyclopse.memory.vault.ulid import generate as gen_ulid


def make_fact(**kwargs) -> VaultFact:
    defaults = {
        "id": gen_ulid(),
        "type": "preference",
        "state": VaultFactState.PROVISIONAL,
        "claim": "User prefers Python over JavaScript",
        "confidence": 0.8,
        "body": "Mentioned this several times in discussion.",
    }
    defaults.update(kwargs)
    return VaultFact(**defaults)


class TestWriteAndRead:
    def test_write_and_read_fact(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)
        loaded = store.read_fact(fact.id)
        assert loaded is not None
        assert loaded.id == fact.id
        assert loaded.claim == fact.claim

    def test_read_nonexistent_returns_none(self, tmp_path):
        store = VaultStore(tmp_path)
        assert store.read_fact(gen_ulid()) is None

    def test_frontmatter_roundtrip(self, tmp_path):
        """All fields survive a write + read cycle."""
        store = VaultStore(tmp_path)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        source = SourceSession(session_id="sess-001", message_range=(0, 5))
        fact = VaultFact(
            id=gen_ulid(),
            type="decision",
            state=VaultFactState.CRYSTALLIZED,
            claim="Use PostgreSQL for the database",
            contrastive="PostgreSQL over MySQL for JSONB support",
            implied=True,
            confidence=0.95,
            reinforcement_count=3,
            surprise_score=0.1,
            written_at=now,
            valid_from=now,
            source_sessions=[source],
            related_to=["RELATEDID1234567890ABCDEF"],
            tier=1,
            body="Decided after evaluating multiple options.",
        )
        store.write_fact(fact)
        loaded = store.read_fact(fact.id)
        assert loaded is not None
        assert loaded.type == "decision"
        assert loaded.state == VaultFactState.CRYSTALLIZED
        assert loaded.contrastive == "PostgreSQL over MySQL for JSONB support"
        assert loaded.implied is True
        assert loaded.confidence == 0.95
        assert loaded.reinforcement_count == 3
        assert loaded.tier == 1
        assert loaded.body.strip() == "Decided after evaluating multiple options."
        assert len(loaded.source_sessions) == 1
        assert loaded.source_sessions[0].session_id == "sess-001"
        assert loaded.source_sessions[0].message_range == (0, 5)
        assert len(loaded.related_to) == 1

    def test_write_to_facts_dir(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)
        assert (tmp_path / "facts" / f"{fact.id}.md").exists()

    def test_archived_fact_goes_to_archive_dir(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(state=VaultFactState.ARCHIVED)
        store.write_fact(fact)
        assert (tmp_path / "archive" / f"{fact.id}.md").exists()
        assert not (tmp_path / "facts" / f"{fact.id}.md").exists()

    def test_atomic_write(self, tmp_path):
        """No .tmp files should be left behind after write."""
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)
        tmp_files = list(tmp_path.rglob("*.tmp"))
        assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"


class TestListFacts:
    def test_list_facts_empty(self, tmp_path):
        store = VaultStore(tmp_path)
        assert store.list_facts() == []

    def test_list_facts_returns_written_facts(self, tmp_path):
        store = VaultStore(tmp_path)
        f1 = make_fact(claim="Fact 1")
        f2 = make_fact(claim="Fact 2")
        store.write_fact(f1)
        store.write_fact(f2)
        facts = store.list_facts()
        assert len(facts) == 2

    def test_list_facts_filter_state(self, tmp_path):
        store = VaultStore(tmp_path)
        prov = make_fact(state=VaultFactState.PROVISIONAL)
        crys = make_fact(state=VaultFactState.CRYSTALLIZED)
        store.write_fact(prov)
        store.write_fact(crys)
        result = store.list_facts(states={VaultFactState.CRYSTALLIZED})
        assert len(result) == 1
        assert result[0].state == VaultFactState.CRYSTALLIZED

    def test_list_facts_filter_type(self, tmp_path):
        store = VaultStore(tmp_path)
        pref = make_fact(type="preference", claim="Pref fact")
        fact = make_fact(type="fact", claim="Regular fact")
        store.write_fact(pref)
        store.write_fact(fact)
        result = store.list_facts(types={"preference"})
        assert len(result) == 1
        assert result[0].type == "preference"

    def test_list_facts_filter_min_confidence(self, tmp_path):
        store = VaultStore(tmp_path)
        low = make_fact(confidence=0.3)
        high = make_fact(confidence=0.9)
        store.write_fact(low)
        store.write_fact(high)
        result = store.list_facts(min_confidence=0.5)
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_list_facts_valid_at(self, tmp_path):
        """Facts with valid_until in the past should be excluded."""
        store = VaultStore(tmp_path)
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=10)
        future = now + timedelta(days=10)

        expired = make_fact(claim="Expired", valid_until=past)
        active = make_fact(claim="Active", valid_until=future)
        forever = make_fact(claim="Forever")

        store.write_fact(expired)
        store.write_fact(active)
        store.write_fact(forever)

        result = store.list_facts(valid_at=now)
        claims = {f.claim for f in result}
        assert "Expired" not in claims
        assert "Active" in claims
        assert "Forever" in claims

    def test_list_facts_source_file_filter(self, tmp_path):
        store = VaultStore(tmp_path)
        f1 = make_fact(source_file="/path/to/doc.md")
        f2 = make_fact(source_file="/other/doc.md")
        store.write_fact(f1)
        store.write_fact(f2)
        result = store.list_facts(source_file="/path/to/doc.md")
        assert len(result) == 1
        assert result[0].source_file == "/path/to/doc.md"


class TestSupersede:
    def test_supersede_fact(self, tmp_path):
        store = VaultStore(tmp_path)
        old = make_fact(claim="Old claim")
        store.write_fact(old)

        new = make_fact(claim="New updated claim")
        old_updated, new_updated = store.supersede_fact(old.id, new)

        assert old_updated.state == VaultFactState.SUPERSEDED
        assert old_updated.superseded_by == new_updated.id
        assert new_updated.supersedes == old.id

        # Old should be in archive
        assert (tmp_path / "archive" / f"{old.id}.md").exists()
        assert not (tmp_path / "facts" / f"{old.id}.md").exists()

        # New should be in facts
        assert (tmp_path / "facts" / f"{new.id}.md").exists()

    def test_supersede_nonexistent_raises(self, tmp_path):
        store = VaultStore(tmp_path)
        new = make_fact(claim="New fact")
        with pytest.raises(FileNotFoundError):
            store.supersede_fact(gen_ulid(), new)


class TestReinforce:
    def test_reinforce_fact(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(reinforcement_count=0)
        store.write_fact(fact)

        session = SourceSession(session_id="sess-abc", message_range=(0, 3))
        updated = store.reinforce_fact(fact.id, session)

        assert updated.reinforcement_count == 1
        assert len(updated.source_sessions) == 1
        assert updated.source_sessions[0].session_id == "sess-abc"

    def test_reinforce_accumulates(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)

        for i in range(3):
            s = SourceSession(session_id=f"sess-{i}", message_range=(i, i + 1))
            store.reinforce_fact(fact.id, s)

        loaded = store.read_fact(fact.id)
        assert loaded.reinforcement_count == 3
        assert len(loaded.source_sessions) == 3

    def test_reinforce_nonexistent_raises(self, tmp_path):
        store = VaultStore(tmp_path)
        s = SourceSession(session_id="x", message_range=(0, 1))
        with pytest.raises(FileNotFoundError):
            store.reinforce_fact(gen_ulid(), s)


class TestArchive:
    def test_archive_fact_moves_file(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)

        assert (tmp_path / "facts" / f"{fact.id}.md").exists()

        store.archive_fact(fact.id, reason="no longer relevant")

        assert (tmp_path / "archive" / f"{fact.id}.md").exists()
        assert not (tmp_path / "facts" / f"{fact.id}.md").exists()

    def test_archive_sets_state(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact()
        store.write_fact(fact)

        archived = store.archive_fact(fact.id)
        assert archived.state == VaultFactState.ARCHIVED

    def test_archive_nonexistent_raises(self, tmp_path):
        store = VaultStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.archive_fact(gen_ulid())


class TestDeleteBySourceFile:
    def test_delete_facts_by_source_file(self, tmp_path):
        store = VaultStore(tmp_path)
        f1 = make_fact(source_file="/docs/notes.md", claim="Fact 1 from notes")
        f2 = make_fact(source_file="/docs/notes.md", claim="Fact 2 from notes")
        f3 = make_fact(source_file="/docs/other.md", claim="Fact from other")
        store.write_fact(f1)
        store.write_fact(f2)
        store.write_fact(f3)

        archived = store.delete_facts_by_source_file("/docs/notes.md")
        assert len(archived) == 2
        assert f1.id in archived
        assert f2.id in archived
        assert f3.id not in archived

        # facts should be in archive
        assert (tmp_path / "archive" / f"{f1.id}.md").exists()
        assert (tmp_path / "archive" / f"{f2.id}.md").exists()
        # other fact still active
        assert (tmp_path / "facts" / f"{f3.id}.md").exists()


class TestStats:
    def test_stats_empty(self, tmp_path):
        store = VaultStore(tmp_path)
        stats = store.get_stats()
        assert stats["total"] == 0
        assert stats["by_state"] == {}

    def test_stats_counts(self, tmp_path):
        store = VaultStore(tmp_path)
        store.write_fact(make_fact(type="preference", state=VaultFactState.PROVISIONAL))
        store.write_fact(make_fact(type="preference", state=VaultFactState.CRYSTALLIZED))
        store.write_fact(make_fact(type="fact", state=VaultFactState.CRYSTALLIZED))

        stats = store.get_stats()
        assert stats["total"] == 3
        assert stats["by_state"]["provisional"] == 1
        assert stats["by_state"]["crystallized"] == 2
        assert stats["by_type"]["preference"] == 2
        assert stats["by_type"]["fact"] == 1
