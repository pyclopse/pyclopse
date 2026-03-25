"""Retrieval profiles and context assembly for vault facts.

Profiles shape how search results are ordered and filtered:
- DEFAULT: balanced, confidence * recency weight
- PLANNING: boost decisions and lessons
- INCIDENT: boost high-confidence facts, sort by confidence desc
- HANDOFF: boost recent facts (written_at desc)
- RESEARCH: boost source_file facts (from documents)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import MemoryType, RetrievalProfile, VaultContext, VaultFact, VaultFactState
from .search import SearchBackend, SearchResult, _STOPWORDS
from .store import VaultStore

# Types that are never auto-injected — agent must query explicitly
_NO_INJECT_TYPES: frozenset[str] = frozenset({MemoryType.RULE.value})

# Keyword boost: each content-word match between query and fact claim/body adds this multiplier
_KEYWORD_BOOST_PER_WORD: float = 0.15
# Maximum total keyword boost (caps at +50% regardless of match count)
_MAX_KEYWORD_BOOST: float = 0.5

# ---------------------------------------------------------------------------
# Profile inference regexes
# ---------------------------------------------------------------------------

INCIDENT_RE = re.compile(
    r"\b(outage|incident|sev[1-4]|p[0-3]|broken|failure|urgent|rollback|"
    r"hotfix|degraded|error|exception|crash|bug)\b",
    re.I,
)
PLANNING_RE = re.compile(
    r"\b(plan|planning|design|architect|roadmap|proposal|spec|migrate|"
    r"migration|approach|strategy|implement)\b",
    re.I,
)
HANDOFF_RE = re.compile(
    r"\b(resume|continue|handoff|pick up|where (did|was) (i|we)|last session|"
    r"where we left|left off|leave off)\b",
    re.I,
)
RESEARCH_RE = re.compile(
    r"\b(research|investigate|study|look into|what do (we|you) know|summarize|overview of)\b",
    re.I,
)


def infer_profile(text: str) -> RetrievalProfile:
    """Auto-infer retrieval profile from message text.

    Checks in priority order: INCIDENT > PLANNING > HANDOFF > RESEARCH > DEFAULT.

    Args:
        text: The message text to classify.

    Returns:
        RetrievalProfile enum value.
    """
    if INCIDENT_RE.search(text):
        return RetrievalProfile.INCIDENT
    if PLANNING_RE.search(text):
        return RetrievalProfile.PLANNING
    if HANDOFF_RE.search(text):
        return RetrievalProfile.HANDOFF
    if RESEARCH_RE.search(text):
        return RetrievalProfile.RESEARCH
    return RetrievalProfile.DEFAULT


# ---------------------------------------------------------------------------
# Context assembler
# ---------------------------------------------------------------------------


class ContextAssembler:
    """Assembles retrieval context from vault facts and memory documents."""

    def __init__(
        self,
        store: VaultStore,
        search: SearchBackend,
        memory_dir: Path,
    ) -> None:
        self._store = store
        self._search = search
        self._memory_dir = memory_dir

    async def assemble(
        self,
        query: str,
        profile: RetrievalProfile = RetrievalProfile.DEFAULT,
        limit: int = 15,
        min_confidence: float = 0.5,
        min_relevance_score: float = 0.0,
        graph_hops: int = 2,
        score_multiplier: float = 1.0,
    ) -> VaultContext:
        """Assemble a VaultContext for the given query and profile.

        Steps:
        1. Search vault facts via backend
        2. Filter: state not superseded/archived, valid_until=None,
                   confidence>=min_confidence, score>=min_relevance_score
        3. Apply profile ordering/caps
        4. Graph expansion: BFS via wikilinks (related_to) up to graph_hops
        5. Scan memory_dir for relevant documents
        6. Return VaultContext
        """
        now = datetime.now(timezone.utc)

        # Content words from the query (stopwords stripped) for keyword de-ranking
        content_words = {
            w for w in re.split(r"\W+", query.lower()) if w and w not in _STOPWORDS
        }

        # Step 1: Search
        search_results = await self._search.search(query, limit=limit * 3)

        # Step 2: Filter
        filtered: list[VaultFact] = []
        seen_ids: set[str] = set()
        for result in search_results:
            fact = result.fact
            if fact is None:
                fact = self._store.read_fact(result.fact_id)
            if fact is None:
                continue

            # Never auto-inject rule type (and other excluded types)
            if fact.type in _NO_INJECT_TYPES:
                continue

            if fact.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
                continue
            if fact.valid_until is not None and fact.valid_until <= now:
                continue
            if fact.confidence < min_confidence:
                continue

            # Apply score adjustments before threshold check:
            # 1. Keyword boost: each content-word found in claim/body raises score
            # 2. score_multiplier: caller-supplied trigger adjustment
            score = result.score
            if content_words:
                claim_lower = fact.claim.lower()
                body_lower = (fact.body or "").lower()
                match_count = sum(
                    1 for w in content_words if w in claim_lower or w in body_lower
                )
                if match_count > 0:
                    boost = min(match_count * _KEYWORD_BOOST_PER_WORD, _MAX_KEYWORD_BOOST)
                    score *= (1.0 + boost)
            score *= score_multiplier

            if min_relevance_score > 0 and score < min_relevance_score:
                continue

            if fact.id not in seen_ids:
                filtered.append(fact)
                seen_ids.add(fact.id)

        # Step 3: Apply profile ordering/cap
        filtered = self._apply_profile_ordering(filtered, profile, limit)
        seen_ids = {f.id for f in filtered}

        # Step 4: Graph expansion via all link types
        if graph_hops > 0 and filtered:
            linked = self._expand_via_links(filtered, seen_ids, graph_hops, min_confidence, now)
            # Append linked facts up to limit total
            remaining = limit - len(filtered)
            filtered = filtered + linked[:remaining]

        # Step 5: Document refs
        document_refs = self._find_document_refs(query)

        return VaultContext(
            facts=filtered,
            document_refs=document_refs,
            profile=profile,
            query=query,
        )

    def _expand_via_links(
        self,
        anchors: list[VaultFact],
        seen_ids: set[str],
        max_hops: int,
        min_confidence: float,
        now,
    ) -> list[VaultFact]:
        """BFS expansion from anchor facts via related_to wikilinks.

        Each hop applies a 0.85 score penalty.  Facts already in seen_ids,
        superseded/archived, expired, or below min_confidence are skipped.

        Returns new facts ordered by descending hop-penalised score.
        """
        # scored_linked: fact_id -> (fact, score)
        scored: dict[str, tuple[VaultFact, float]] = {}
        # BFS queue: (fact, hop_number)
        queue: list[tuple[VaultFact, int]] = [(f, 0) for f in anchors]
        visited: set[str] = set(seen_ids)

        while queue:
            current, hop = queue.pop(0)
            if hop >= max_hops:
                continue
            # Follow all link types: related_to (generic), depends_on, part_of
            # Skip contradicts — those facts are superseded/archived and already filtered
            follow_ids = (
                list(current.related_to)
                + list(current.depends_on)
                + ([current.part_of] if current.part_of else [])
            )
            for related_id in follow_ids:
                if related_id in visited:
                    continue
                visited.add(related_id)
                fact = self._store.read_fact(related_id)
                if fact is None:
                    continue
                if fact.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
                    continue
                if fact.valid_until is not None and fact.valid_until <= now:
                    continue
                if fact.confidence < min_confidence:
                    continue
                link_score = fact.confidence * (0.85 ** (hop + 1))
                if related_id not in scored or scored[related_id][1] < link_score:
                    scored[related_id] = (fact, link_score)
                queue.append((fact, hop + 1))

        return [f for f, _ in sorted(scored.values(), key=lambda x: x[1], reverse=True)]

    def _apply_profile_ordering(
        self,
        facts: list[VaultFact],
        profile: RetrievalProfile,
        limit: int,
    ) -> list[VaultFact]:
        """Sort and cap facts based on the retrieval profile."""
        now = datetime.now(timezone.utc)

        def recency_weight(fact: VaultFact) -> float:
            age_days = max(0, (now - fact.written_at).total_seconds() / 86400)
            return 1.0 / (1.0 + age_days / 30.0)

        if profile == RetrievalProfile.DEFAULT:
            facts.sort(
                key=lambda f: f.confidence * recency_weight(f),
                reverse=True,
            )

        elif profile == RetrievalProfile.PLANNING:
            planning_types = {"decision", "lesson"}
            facts.sort(
                key=lambda f: (
                    2.0 if f.type in planning_types else 1.0
                ) * f.confidence,
                reverse=True,
            )

        elif profile == RetrievalProfile.INCIDENT:
            facts.sort(key=lambda f: f.confidence, reverse=True)

        elif profile == RetrievalProfile.HANDOFF:
            facts.sort(key=lambda f: f.written_at, reverse=True)

        elif profile == RetrievalProfile.RESEARCH:
            facts.sort(
                key=lambda f: (
                    2.0 if f.source_file is not None else 1.0
                ) * f.confidence,
                reverse=True,
            )

        return facts[:limit]

    def _find_document_refs(self, query: str) -> list[str]:
        """Scan memory_dir for documents with filenames matching query keywords."""
        if not self._memory_dir.exists():
            return []

        query_words = [w.lower() for w in re.split(r"\W+", query) if len(w) >= 3]
        if not query_words:
            return []

        refs = []
        for md_file in self._memory_dir.glob("*.md"):
            stem_lower = md_file.stem.lower()
            if any(word in stem_lower for word in query_words):
                refs.append(str(md_file))

        return refs[:5]  # cap at 5 document refs

    def format_for_injection(self, context: VaultContext) -> str:
        """Format VaultContext as an XML block for system prompt injection.

        Returns:
            XML string ready to inject into a system prompt.

        Example::

            <vault_context source="pyclawops-vault" profile="planning">
            <facts>
            - [decision] PostgreSQL chosen for database (confidence: 0.95, reinforced 3x)
            - [preference] Prefers 4-space indentation (confidence: 0.85)
            </facts>
            <documents>
            - research-xyz.md: "Research notes on XYZ framework..."
            </documents>
            </vault_context>
        """
        if not context.facts and not context.document_refs:
            return ""

        lines = [
            f'<vault_context source="pyclawops-vault" profile="{context.profile.value}">'
        ]

        if context.facts:
            fact_map = {f.id: f for f in context.facts}
            lines.append("<facts>")
            for fact in context.facts:
                reinforced = ""
                if fact.reinforcement_count > 0:
                    reinforced = f", reinforced {fact.reinforcement_count}x"
                lines.append(
                    f"- [{fact.type}] {fact.claim} "
                    f"(confidence: {fact.confidence:.2f}{reinforced})"
                )
                # Surface typed relationships for richer context
                for dep_id in fact.depends_on[:2]:
                    dep = fact_map.get(dep_id) or self._store.read_fact(dep_id)
                    if dep:
                        lines.append(f"  → depends on: [{dep.type}] {dep.claim}")
                if fact.part_of:
                    parent = fact_map.get(fact.part_of) or self._store.read_fact(fact.part_of)
                    if parent:
                        lines.append(f"  → part of: [{parent.type}] {parent.claim}")
            lines.append("</facts>")

        if context.document_refs:
            lines.append("<documents>")
            for ref in context.document_refs:
                p = Path(ref)
                lines.append(f'- {p.name}: "{p.stem.replace("-", " ").replace("_", " ").title()}"')
            lines.append("</documents>")

        lines.append("</vault_context>")
        return "\n".join(lines)
