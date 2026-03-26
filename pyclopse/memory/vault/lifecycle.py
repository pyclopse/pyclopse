"""LifecycleManager — background maintenance tasks for vault facts.

Handles:
- Crystallization: provisional facts that have been reinforced or aged become crystallized
- Forgetting: unreinforced provisional facts that have aged are archived
- Hypothesis management: promote or archive hypotheses
- Tier compression: compress body/claim over time to save space
- Anti-memory reaping: expire anti-type facts past their expiry date
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    LifecycleStats,
    MemoryType,
    VaultFact,
    VaultFactState,
)
from .store import VaultStore

logger = logging.getLogger("pyclopse.vault.lifecycle")

_DEFAULTS = {
    "crystallize_reinforcements": 3,
    "crystallize_days": 7,
    "forget_days": 30,
    "tier1_to_2_days": 30,
    "tier2_to_3_days": 90,
    "tier3_to_4_days": 365,
}


class LifecycleManager:
    """Runs all background maintenance tasks for vault facts."""

    def __init__(
        self,
        vault_dir: Path,
        store: VaultStore,
        config: Optional[dict] = None,  # type: ignore[name-defined]
    ) -> None:
        self._vault_dir = vault_dir
        self._store = store
        cfg = config or {}
        self._crystallize_reinforcements: int = int(
            cfg.get("crystallize_reinforcements", _DEFAULTS["crystallize_reinforcements"])
        )
        self._crystallize_days: int = int(
            cfg.get("crystallize_days", _DEFAULTS["crystallize_days"])
        )
        self._forget_days: int = int(
            cfg.get("forget_days", _DEFAULTS["forget_days"])
        )
        self._tier1_to_2_days: int = int(
            cfg.get("tier1_to_2_days", _DEFAULTS["tier1_to_2_days"])
        )
        self._tier2_to_3_days: int = int(
            cfg.get("tier2_to_3_days", _DEFAULTS["tier2_to_3_days"])
        )
        self._tier3_to_4_days: int = int(
            cfg.get("tier3_to_4_days", _DEFAULTS["tier3_to_4_days"])
        )

    def run_crystallization(self) -> LifecycleStats:
        """Promote provisional facts to crystallized or archive forgotten ones.

        Rules for provisional facts:
        - reinforcement_count >= crystallize_reinforcements → crystallized
        - age > crystallize_days → crystallized
        - age > forget_days AND reinforcement_count == 0 → archived (forgotten)

        Rules for hypothesis facts:
        - reinforcement_count >= 2 → promote to provisional (keep type)
        - age > 14 days AND reinforcement_count == 0 → archived
        """
        stats = LifecycleStats()
        now = datetime.now(timezone.utc)

        # Process provisional facts
        provisional_facts = self._store.list_facts(
            states={VaultFactState.PROVISIONAL}
        )
        for fact in provisional_facts:
            age_days = (now - fact.written_at).days
            was_changed = False

            # Forget first (takes priority over crystallize for very old facts)
            if age_days > self._forget_days and fact.reinforcement_count == 0:
                self._store.archive_fact(fact.id, reason="forgotten: unreinforced after forget_days")
                stats.forgotten += 1
                was_changed = True
                logger.debug("Forgot provisional fact %s (age=%d days)", fact.id, age_days)

            elif fact.reinforcement_count >= self._crystallize_reinforcements:
                self._store.update_fact(fact.id, state=VaultFactState.CRYSTALLIZED)
                stats.crystallized += 1
                was_changed = True
                logger.debug(
                    "Crystallized fact %s by reinforcement (count=%d)",
                    fact.id,
                    fact.reinforcement_count,
                )

            elif age_days > self._crystallize_days:
                self._store.update_fact(fact.id, state=VaultFactState.CRYSTALLIZED)
                stats.crystallized += 1
                was_changed = True
                logger.debug("Crystallized fact %s by age (age=%d days)", fact.id, age_days)

        # Process hypothesis facts
        hypothesis_facts = self._store.list_facts(
            types={MemoryType.HYPOTHESIS}
        )
        for fact in hypothesis_facts:
            if fact.state in (VaultFactState.ARCHIVED, VaultFactState.SUPERSEDED):
                continue

            age_days = (now - fact.written_at).days

            if fact.reinforcement_count >= 2:
                # Promote to provisional
                self._store.update_fact(fact.id, state=VaultFactState.PROVISIONAL)
                stats.hypotheses_promoted += 1
                logger.debug("Promoted hypothesis %s to provisional", fact.id)

            elif age_days > 14 and fact.reinforcement_count == 0:
                # Archive stale hypothesis
                self._store.archive_fact(fact.id, reason="stale hypothesis")
                stats.hypotheses_archived += 1
                logger.debug("Archived stale hypothesis %s (age=%d days)", fact.id, age_days)

        return stats

    def run_tier_compression(self) -> LifecycleStats:
        """Compress fact bodies/claims over time.

        Rules for crystallized facts:
        - tier=1 AND age > tier1_to_2_days: compress body to empty string, tier=2
        - tier=2 AND age > tier2_to_3_days: compress claim to summary, tier=3
        - tier=3 AND age > tier3_to_4_days: tier=4 (ultra-minimal)

        Provenance (source_sessions, source_file) is NEVER removed.
        """
        stats = LifecycleStats()
        now = datetime.now(timezone.utc)

        crystallized_facts = self._store.list_facts(
            states={VaultFactState.CRYSTALLIZED}
        )

        for fact in crystallized_facts:
            age_days = (now - fact.written_at).days

            if fact.tier == 1 and age_days > self._tier1_to_2_days:
                self._store.update_fact(fact.id, body="", tier=2)
                stats.compressed += 1
                logger.debug("Compressed fact %s tier 1→2 (age=%d days)", fact.id, age_days)

            elif fact.tier == 2 and age_days > self._tier2_to_3_days:
                # Compress claim to a brief summary
                summary = fact.claim[:80] + "..." if len(fact.claim) > 80 else fact.claim
                self._store.update_fact(fact.id, claim=summary, tier=3)
                stats.compressed += 1
                logger.debug("Compressed fact %s tier 2→3 (age=%d days)", fact.id, age_days)

            elif fact.tier == 3 and age_days > self._tier3_to_4_days:
                self._store.update_fact(fact.id, tier=4)
                stats.compressed += 1
                logger.debug("Compressed fact %s tier 3→4 (age=%d days)", fact.id, age_days)

        return stats

    def run_anti_memory_reaper(self) -> LifecycleStats:
        """Archive all anti-type facts where expires_at <= now."""
        stats = LifecycleStats()
        now = datetime.now(timezone.utc)

        anti_facts = self._store.list_facts(types={MemoryType.ANTI})
        for fact in anti_facts:
            if fact.state in (VaultFactState.ARCHIVED, VaultFactState.SUPERSEDED):
                continue
            if fact.expires_at is not None and fact.expires_at <= now:
                self._store.archive_fact(fact.id, reason="anti-memory expired")
                stats.reaped += 1
                logger.debug("Reaped expired anti-memory %s", fact.id)

        return stats

    def run_all(self) -> LifecycleStats:
        """Run all lifecycle tasks. Returns combined stats."""
        stats = LifecycleStats()
        stats = stats.merge(self.run_crystallization())
        stats = stats.merge(self.run_tier_compression())
        stats = stats.merge(self.run_anti_memory_reaper())
        return stats
