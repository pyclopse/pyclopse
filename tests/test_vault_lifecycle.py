"""Tests for LifecycleManager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pyclawops.memory.vault.lifecycle import LifecycleManager
from pyclawops.memory.vault.models import (
    MemoryType,
    VaultFact,
    VaultFactState,
)
from pyclawops.memory.vault.store import VaultStore
from pyclawops.memory.vault.ulid import generate as gen_ulid


def make_fact(age_days: int = 0, **kwargs) -> VaultFact:
    """Create a fact with written_at offset by age_days into the past."""
    written_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    defaults = {
        "id": gen_ulid(),
        "type": "preference",
        "state": VaultFactState.PROVISIONAL,
        "claim": "Test claim",
        "confidence": 0.7,
        "written_at": written_at,
    }
    defaults.update(kwargs)
    return VaultFact(**defaults)


class TestCrystallization:
    def test_crystallize_by_reinforcement(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(reinforcement_count=3)
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"crystallize_reinforcements": 3})
        stats = mgr.run_crystallization()

        assert stats.crystallized == 1
        loaded = store.read_fact(fact.id)
        assert loaded.state == VaultFactState.CRYSTALLIZED

    def test_crystallize_by_age(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(age_days=10)
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"crystallize_days": 7})
        stats = mgr.run_crystallization()

        assert stats.crystallized == 1
        loaded = store.read_fact(fact.id)
        assert loaded.state == VaultFactState.CRYSTALLIZED

    def test_young_fact_not_crystallized(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(age_days=2)
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"crystallize_days": 7, "forget_days": 30})
        stats = mgr.run_crystallization()

        assert stats.crystallized == 0
        loaded = store.read_fact(fact.id)
        assert loaded.state == VaultFactState.PROVISIONAL

    def test_forget_unreinforced_provisional(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(age_days=35, reinforcement_count=0)
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"forget_days": 30, "crystallize_days": 7})
        stats = mgr.run_crystallization()

        assert stats.forgotten == 1
        loaded = store.read_fact(fact.id)
        assert loaded.state == VaultFactState.ARCHIVED

    def test_reinforced_fact_not_forgotten(self, tmp_path):
        """A reinforced old fact should be crystallized, not forgotten."""
        store = VaultStore(tmp_path)
        fact = make_fact(age_days=35, reinforcement_count=5)
        store.write_fact(fact)

        mgr = LifecycleManager(
            tmp_path, store,
            {"forget_days": 30, "crystallize_days": 7, "crystallize_reinforcements": 3},
        )
        stats = mgr.run_crystallization()

        assert stats.crystallized == 1
        assert stats.forgotten == 0

    def test_crystallized_fact_not_reprocessed(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(age_days=20, state=VaultFactState.CRYSTALLIZED)
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_crystallization()

        # Already crystallized — no change
        assert stats.crystallized == 0

    def test_hypothesis_promotion(self, tmp_path):
        store = VaultStore(tmp_path)
        h = make_fact(type=MemoryType.HYPOTHESIS, reinforcement_count=2, state=VaultFactState.PROVISIONAL)
        store.write_fact(h)

        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_crystallization()

        assert stats.hypotheses_promoted == 1
        loaded = store.read_fact(h.id)
        assert loaded.state == VaultFactState.PROVISIONAL

    def test_hypothesis_archival_when_stale(self, tmp_path):
        store = VaultStore(tmp_path)
        h = make_fact(
            type=MemoryType.HYPOTHESIS,
            reinforcement_count=0,
            age_days=20,
            state=VaultFactState.PROVISIONAL,
        )
        store.write_fact(h)

        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_crystallization()

        assert stats.hypotheses_archived == 1
        loaded = store.read_fact(h.id)
        assert loaded.state == VaultFactState.ARCHIVED


class TestTierCompression:
    def test_tier_compression_1_to_2(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(
            age_days=35,
            state=VaultFactState.CRYSTALLIZED,
            tier=1,
            body="This is a long narrative body that should be compressed away.",
        )
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"tier1_to_2_days": 30})
        stats = mgr.run_tier_compression()

        assert stats.compressed == 1
        loaded = store.read_fact(fact.id)
        assert loaded.tier == 2
        assert loaded.body == ""

    def test_tier_compression_2_to_3(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(
            age_days=100,
            state=VaultFactState.CRYSTALLIZED,
            tier=2,
            claim="A" * 100,  # long claim that will be truncated
        )
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"tier2_to_3_days": 90})
        stats = mgr.run_tier_compression()

        assert stats.compressed == 1
        loaded = store.read_fact(fact.id)
        assert loaded.tier == 3
        assert len(loaded.claim) <= 83  # max 80 + "..."

    def test_tier_compression_3_to_4(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(
            age_days=400,
            state=VaultFactState.CRYSTALLIZED,
            tier=3,
        )
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"tier3_to_4_days": 365})
        stats = mgr.run_tier_compression()

        assert stats.compressed == 1
        loaded = store.read_fact(fact.id)
        assert loaded.tier == 4

    def test_provisional_not_compressed(self, tmp_path):
        store = VaultStore(tmp_path)
        fact = make_fact(
            age_days=100,
            state=VaultFactState.PROVISIONAL,
            tier=1,
        )
        store.write_fact(fact)

        mgr = LifecycleManager(tmp_path, store, {"tier1_to_2_days": 30})
        stats = mgr.run_tier_compression()

        assert stats.compressed == 0


class TestAntiMemoryReaper:
    def test_anti_memory_reaper(self, tmp_path):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        store = VaultStore(tmp_path)

        expired = make_fact(
            type=MemoryType.ANTI,
            expires_at=now - timedelta(hours=1),
            state=VaultFactState.PROVISIONAL,
        )
        active = make_fact(
            type=MemoryType.ANTI,
            expires_at=now + timedelta(days=7),
            state=VaultFactState.PROVISIONAL,
        )
        store.write_fact(expired)
        store.write_fact(active)

        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_anti_memory_reaper()

        assert stats.reaped == 1
        loaded_expired = store.read_fact(expired.id)
        assert loaded_expired.state == VaultFactState.ARCHIVED

        loaded_active = store.read_fact(active.id)
        assert loaded_active.state == VaultFactState.PROVISIONAL

    def test_anti_memory_no_expiry_not_reaped(self, tmp_path):
        store = VaultStore(tmp_path)
        anti = make_fact(type=MemoryType.ANTI, expires_at=None, state=VaultFactState.PROVISIONAL)
        store.write_fact(anti)

        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_anti_memory_reaper()
        assert stats.reaped == 0


class TestRunAll:
    def test_run_all_returns_stats(self, tmp_path):
        store = VaultStore(tmp_path)
        mgr = LifecycleManager(tmp_path, store)
        stats = mgr.run_all()
        # Should return a LifecycleStats (even if all zeros)
        from pyclawops.memory.vault.models import LifecycleStats
        assert isinstance(stats, LifecycleStats)

    def test_run_all_combines_stats(self, tmp_path):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        store = VaultStore(tmp_path)

        # Fact that will be crystallized
        old_prov = make_fact(age_days=10, state=VaultFactState.PROVISIONAL)
        store.write_fact(old_prov)

        # Anti-memory that will be reaped
        anti = make_fact(
            type=MemoryType.ANTI,
            expires_at=now - timedelta(hours=1),
            state=VaultFactState.PROVISIONAL,
        )
        store.write_fact(anti)

        mgr = LifecycleManager(tmp_path, store, {"crystallize_days": 7})
        stats = mgr.run_all()

        assert stats.crystallized >= 1
        assert stats.reaped >= 1
