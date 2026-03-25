"""Tests for vault retrieval: profile inference and ContextAssembler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pyclaw.memory.vault.models import (
    RetrievalProfile,
    VaultFact,
    VaultFactState,
)
from pyclaw.memory.vault.retrieval import ContextAssembler, infer_profile
from pyclaw.memory.vault.search import FallbackSearchBackend
from pyclaw.memory.vault.store import VaultStore
from pyclaw.memory.vault.ulid import generate as gen_ulid


def make_crystallized_fact(**kwargs) -> VaultFact:
    defaults = {
        "id": gen_ulid(),
        "type": "fact",
        "state": VaultFactState.CRYSTALLIZED,
        "claim": "Test fact",
        "confidence": 0.8,
    }
    defaults.update(kwargs)
    return VaultFact(**defaults)


class TestInferProfile:
    def test_infer_profile_incident(self):
        texts = [
            "there's an urgent outage in production",
            "sev1 incident affecting all users",
            "the service is broken and degraded",
            "we need a hotfix for this crash",
        ]
        for text in texts:
            assert infer_profile(text) == RetrievalProfile.INCIDENT, f"Failed for: {text}"

    def test_infer_profile_planning(self):
        texts = [
            "let's design the architecture",
            "working on the migration strategy",
            "creating a roadmap for Q2",
            "planning the implementation approach",
        ]
        for text in texts:
            assert infer_profile(text) == RetrievalProfile.PLANNING, f"Failed for: {text}"

    def test_infer_profile_handoff(self):
        texts = [
            "resume from last session",
            "where did we leave off",
            "let's continue from where we were",
            "where was I",
        ]
        for text in texts:
            assert infer_profile(text) == RetrievalProfile.HANDOFF, f"Failed for: {text}"

    def test_infer_profile_research(self):
        texts = [
            "what do you know about Python async",
            "give me an overview of Redis",
            "research the options for caching",
            "investigate what tools are available",
        ]
        for text in texts:
            assert infer_profile(text) == RetrievalProfile.RESEARCH, f"Failed for: {text}"

    def test_infer_profile_default(self):
        texts = [
            "hello, how are you today",
            "can you help me with a task",
            "just a regular message",
            "",
        ]
        for text in texts:
            assert infer_profile(text) == RetrievalProfile.DEFAULT, f"Failed for: {text}"

    def test_incident_takes_priority_over_planning(self):
        """If both patterns match, INCIDENT wins."""
        text = "urgent incident - need to plan the rollback strategy"
        assert infer_profile(text) == RetrievalProfile.INCIDENT


class TestContextAssembler:
    def _make_assembler(self, tmp_path):
        store = VaultStore(tmp_path)
        search = FallbackSearchBackend(store)
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        return ContextAssembler(store, search, memory_dir), store

    async def test_format_for_injection(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)
        fact = make_crystallized_fact(
            claim="User prefers Python",
            type="preference",
            reinforcement_count=2,
        )
        store.write_fact(fact)
        await assembler._search.index_fact(fact)

        ctx = await assembler.assemble("Python preference", limit=10)
        output = assembler.format_for_injection(ctx)

        assert "<vault_context" in output
        assert "pyclaw-vault" in output
        assert "</vault_context>" in output

    async def test_format_empty_context_returns_empty_string(self, tmp_path):
        assembler, _ = self._make_assembler(tmp_path)
        from pyclaw.memory.vault.models import VaultContext
        ctx = VaultContext(facts=[], document_refs=[], profile=RetrievalProfile.DEFAULT, query="test")
        result = assembler.format_for_injection(ctx)
        assert result == ""

    async def test_assemble_filters_superseded(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)

        active = make_crystallized_fact(claim="Active Python fact", type="fact")
        superseded = make_crystallized_fact(
            claim="Old Python fact",
            state=VaultFactState.SUPERSEDED,
        )
        store.write_fact(active)
        store.write_fact(superseded)
        await assembler._search.index_fact(active)
        await assembler._search.index_fact(superseded)

        ctx = await assembler.assemble("Python", limit=10)
        fact_ids = {f.id for f in ctx.facts}
        assert active.id in fact_ids
        assert superseded.id not in fact_ids

    async def test_assemble_filters_low_confidence(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)

        high = make_crystallized_fact(claim="High confidence Python fact", confidence=0.9)
        low = make_crystallized_fact(claim="Low confidence Python fact", confidence=0.2)
        store.write_fact(high)
        store.write_fact(low)
        await assembler._search.index_fact(high)
        await assembler._search.index_fact(low)

        ctx = await assembler.assemble("Python", min_confidence=0.5, limit=10)
        fact_ids = {f.id for f in ctx.facts}
        assert high.id in fact_ids
        assert low.id not in fact_ids

    async def test_assemble_planning_boosts_decisions(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)

        decision = make_crystallized_fact(
            claim="We decided to use Python",
            type="decision",
            confidence=0.8,
        )
        regular = make_crystallized_fact(
            claim="Python is popular",
            type="fact",
            confidence=0.8,
        )
        store.write_fact(decision)
        store.write_fact(regular)
        await assembler._search.index_fact(decision)
        await assembler._search.index_fact(regular)

        ctx = await assembler.assemble(
            "Python technology choice",
            profile=RetrievalProfile.PLANNING,
            limit=10,
        )
        # Decision type should appear before regular fact
        if len(ctx.facts) >= 2:
            types = [f.type for f in ctx.facts]
            decision_idx = types.index("decision") if "decision" in types else -1
            fact_idx = types.index("fact") if "fact" in types else -1
            if decision_idx >= 0 and fact_idx >= 0:
                assert decision_idx <= fact_idx, "Decision should be boosted before regular facts"

    async def test_assemble_respects_valid_until(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)

        now = datetime.now(timezone.utc)
        expired = make_crystallized_fact(
            claim="Expired Python fact",
            valid_until=now - timedelta(days=1),
        )
        store.write_fact(expired)
        await assembler._search.index_fact(expired)

        ctx = await assembler.assemble("Python", limit=10)
        assert expired.id not in {f.id for f in ctx.facts}

    async def test_assemble_returns_vault_context(self, tmp_path):
        from pyclaw.memory.vault.models import VaultContext
        assembler, _ = self._make_assembler(tmp_path)
        ctx = await assembler.assemble("test query")
        assert isinstance(ctx, VaultContext)

    def test_format_includes_type_and_claim(self, tmp_path):
        assembler, store = self._make_assembler(tmp_path)
        from pyclaw.memory.vault.models import VaultContext

        fact = make_crystallized_fact(
            claim="User always uses tabs",
            type="preference",
        )
        ctx = VaultContext(facts=[fact], document_refs=[], profile=RetrievalProfile.DEFAULT, query="")
        output = assembler.format_for_injection(ctx)
        assert "[preference]" in output
        assert "User always uses tabs" in output
