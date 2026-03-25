"""Search backends for vault facts.

Provides an abstract SearchBackend interface, a FallbackSearchBackend
that performs keyword-based search with no external dependencies, a
QmdSearchBackend that uses the qmd binary for BM25+vector search, and a
HybridSearchBackend that runs both in parallel and merges results via
Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

import re
import shutil
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

from .models import VaultFact


class SearchResult(BaseModel):
    """A single search result from a vault search query."""

    fact_id: str
    score: float
    fact: Optional[VaultFact] = None


class SearchBackend(ABC):
    """Abstract interface for vault search backends."""

    @abstractmethod
    async def index_fact(self, fact: VaultFact) -> None:
        """Add or update a fact in the search index."""

    @abstractmethod
    async def remove_fact(self, fact_id: str) -> None:
        """Remove a fact from the search index."""

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 20,
        filters: Optional[dict] = None,
    ) -> list[SearchResult]:
        """Search for facts matching the query.

        Args:
            query: Natural-language or keyword search string.
            limit: Maximum number of results to return.
            filters: Optional dict of additional filter criteria.

        Returns:
            List of SearchResult ordered by descending score.
        """

    @abstractmethod
    async def reindex_all(self, facts: list[VaultFact]) -> None:
        """Rebuild the search index from scratch."""


# Common English stopwords stripped from queries before keyword scoring.
# Articles, prepositions, conjunctions, and high-frequency verbs that appear
# in almost every sentence add noise without signal.
_STOPWORDS: frozenset[str] = frozenset({
    # articles
    "a", "an", "the",
    # prepositions
    "at", "by", "for", "from", "in", "into", "of", "on", "onto", "out",
    "over", "per", "to", "up", "via", "with", "within", "without",
    # conjunctions
    "and", "as", "but", "if", "nor", "or", "so", "than", "that", "though",
    "until", "when", "where", "while", "yet",
    # common verbs/auxiliaries
    "am", "are", "be", "been", "being", "can", "could", "did", "do", "does",
    "had", "has", "have", "is", "may", "might", "must", "shall", "should",
    "was", "were", "will", "would",
    # pronouns & determiners
    "all", "any", "each", "every", "he", "her", "him", "his", "i", "it",
    "its", "me", "my", "no", "not", "our", "she", "some", "their", "them",
    "they", "this", "those", "us", "we", "what", "which", "who", "you",
    # misc high-frequency filler
    "about", "also", "both", "just", "more", "now", "only", "other",
    "same", "such", "then", "there", "these", "too", "very",
})


class FallbackSearchBackend(SearchBackend):
    """Simple keyword search over fact claims. No external dependencies.

    Scoring strategy (quality bonuses only apply when relevance > 0):
    - Exact phrase match in claim: +10 relevance
    - Per-word match in claim: +1 per content word
    - Body text keyword match: +0.5 per content word
    - Confidence bonus: confidence * 2 (tiebreaker)
    - Tier penalty: (tier-1) * 0.5 (older tiers rank lower)
    - Reinforcement bonus: up to +1.0

    Stopwords (articles, prepositions, common verbs, etc.) are stripped from
    the query before per-word scoring so that words like "is", "a", "for"
    don't inflate scores for unrelated high-confidence facts.

    Facts with zero keyword relevance are excluded entirely — confidence
    alone does not cause a fact to appear in results.

    In-memory index is built from VaultStore on demand and updated
    via index_fact/remove_fact.
    """

    def __init__(self, store) -> None:  # store: VaultStore (avoid circular import)
        self._store = store
        # In-memory index: fact_id -> VaultFact
        self._index: dict[str, VaultFact] = {}
        self._bootstrapped = False

    def _ensure_bootstrapped(self) -> None:
        """Load all active facts into the in-memory index on first use."""
        if self._bootstrapped:
            return
        facts = self._store.list_facts()
        for fact in facts:
            self._index[fact.id] = fact
        self._bootstrapped = True

    async def index_fact(self, fact: VaultFact) -> None:
        """Add or update a fact in the in-memory index."""
        self._index[fact.id] = fact

    async def remove_fact(self, fact_id: str) -> None:
        """Remove a fact from the in-memory index."""
        self._index.pop(fact_id, None)

    async def search(
        self,
        query: str,
        limit: int = 20,
        filters: Optional[dict] = None,
    ) -> list[SearchResult]:
        """Keyword search over indexed fact claims and bodies."""
        self._ensure_bootstrapped()

        if not query.strip():
            return []

        query_lower = query.lower()
        all_words = [w for w in re.split(r"\W+", query_lower) if w]
        # Strip stopwords for per-word scoring; keep full phrase for exact-match check
        content_words = [w for w in all_words if w not in _STOPWORDS]
        if not all_words:
            return []

        scored: list[SearchResult] = []

        for fact_id, fact in self._index.items():
            claim_lower = fact.claim.lower()
            body_lower = (fact.body or "").lower()

            # Relevance score — keyword hits only
            relevance = 0.0

            # Exact phrase match (uses full query including stopwords)
            if query_lower in claim_lower:
                relevance += 10.0

            # Per-word scoring uses only content words (stopwords excluded)
            for word in content_words:
                if word in claim_lower:
                    relevance += 1.0

            # Body keyword scoring (lower weight, content words only)
            for word in content_words:
                if word in body_lower:
                    relevance += 0.5

            # Quality bonuses only apply when there is at least some relevance.
            # This prevents high-confidence but irrelevant facts from flooding results.
            if relevance <= 0:
                continue

            score = relevance
            score += fact.confidence * 2.0
            score -= (fact.tier - 1) * 0.5
            score += min(fact.reinforcement_count * 0.2, 1.0)

            # Normalize to [0, 1]: reference ceiling is a ~2-word exact-phrase match
            # (10 + 2 words + 1 body = 13) + max quality (2 + 1 = 3) = 16.
            # This ensures a single specific-word hit scores ~0.3 with decent confidence.
            # Cap at 1.0 for anything exceeding the reference.
            normalized = min(score / 16.0, 1.0)
            scored.append(SearchResult(fact_id=fact_id, score=normalized, fact=fact))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:limit]

    async def reindex_all(self, facts: list[VaultFact]) -> None:
        """Rebuild the in-memory index from the provided fact list."""
        self._index = {f.id: f for f in facts}
        self._bootstrapped = True


class QmdSearchBackend(SearchBackend):
    """Semantic search backend using the qmd binary (BM25 + vector + reranking).

    Shells out to `qmd query --json` against a named QMD collection that is
    expected to point directly at the vault facts directory.  The fact files
    are already on disk (written by VaultStore), so index_fact / remove_fact
    just trigger a `qmd update && qmd embed` refresh.

    A background debounce task coalesces rapid index_fact / remove_fact calls
    into a single qmd update + embed run, avoiding hammering the subprocess on
    bulk ingestion.

    Args:
        collection: QMD collection name (e.g. ``"niggy-vault"``).
        qmd_path: Path to the qmd binary. Defaults to ``"qmd"`` (on PATH).
        store: VaultStore used to resolve fact IDs from file paths in QMD results.
        debounce_seconds: How long to wait after the last index/remove call
            before running qmd update+embed. Defaults to 5.0.
    """

    def __init__(
        self,
        collection: str,
        store,  # VaultStore — avoid circular import
        qmd_path: str = "qmd",
        debounce_seconds: float = 5.0,
    ) -> None:
        import asyncio
        import logging
        self._collection = collection
        self._store = store
        self._qmd = qmd_path
        self._debounce = debounce_seconds
        self._pending_update: bool = False
        self._update_task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger("pyclaw.memory.vault.qmd")

    def _schedule_update(self) -> None:
        """Schedule a debounced qmd update+embed run."""
        import asyncio
        self._pending_update = True
        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()
        self._update_task = asyncio.create_task(self._debounced_update())

    async def _debounced_update(self) -> None:
        import asyncio
        try:
            await asyncio.sleep(self._debounce)
            await self._run_update()
        except asyncio.CancelledError:
            pass

    async def _ensure_collection(self) -> bool:
        """Create the QMD collection if it doesn't exist yet.

        Returns True if the collection exists or was just created, False on error.
        """
        import asyncio
        try:
            # Check if collection exists by running a zero-result query
            proc = await asyncio.create_subprocess_exec(
                self._qmd, "collection", "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if f"qmd://{self._collection}/".encode() in stdout:
                return True
            # Collection is missing — create it from the facts directory on disk
            facts_dir = self._store.facts_dir
            if not facts_dir.exists():
                return False
            proc = await asyncio.create_subprocess_exec(
                self._qmd, "collection", "add", str(facts_dir),
                "--name", self._collection,
                "--mask", "*.md",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            self._logger.info(
                "qmd collection %r auto-created from %s", self._collection, facts_dir
            )
            return True
        except Exception as exc:
            self._logger.warning("qmd ensure_collection failed: %s", exc)
            return False

    async def _run_update(self) -> None:
        """Run qmd update then qmd embed on the collection."""
        import asyncio
        try:
            await self._ensure_collection()
            proc = await asyncio.create_subprocess_exec(
                self._qmd, "update",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            proc = await asyncio.create_subprocess_exec(
                self._qmd, "embed",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            self._pending_update = False
            self._logger.debug("qmd update+embed completed for collection %r", self._collection)
        except Exception as exc:
            self._logger.warning("qmd update failed: %s", exc)

    async def index_fact(self, fact: VaultFact) -> None:
        """Fact file is already on disk; schedule a qmd refresh."""
        self._schedule_update()

    async def remove_fact(self, fact_id: str) -> None:
        """Fact file is already deleted; schedule a qmd refresh."""
        self._schedule_update()

    async def search(
        self,
        query: str,
        limit: int = 20,
        filters: Optional[dict] = None,
    ) -> list[SearchResult]:
        """Run qmd query --json and map results back to SearchResult objects."""
        import asyncio
        import json as _json

        if not query.strip():
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                self._qmd, "query", "--json",
                "-n", str(limit),
                "-c", self._collection,
                query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except Exception as exc:
            self._logger.warning("qmd query failed: %s", exc)
            return []

        try:
            raw = stdout.decode("utf-8", errors="replace").strip()
            # qmd sometimes emits progress/tip lines before the JSON array
            start = raw.find("[")
            if start == -1:
                return []
            data = _json.loads(raw[start:])
        except Exception as exc:
            self._logger.warning("qmd JSON parse failed: %s", exc)
            return []

        results: list[SearchResult] = []
        all_facts = {f.id: f for f in self._store.list_facts()}

        for item in data:
            file_path = item.get("file", "")
            # Extract fact ID from filename: qmd://niggy-vault/01KMXXX.md
            # QMD lowercases filenames in its index — uppercase to match ULID fact IDs
            fact_id = file_path.rsplit("/", 1)[-1].replace(".md", "").upper()
            fact = all_facts.get(fact_id)
            if fact is None:
                continue
            results.append(SearchResult(
                fact_id=fact_id,
                score=float(item.get("score", 0.0)),
                fact=fact,
            ))

        return results

    async def reindex_all(self, facts: list[VaultFact]) -> None:
        """Trigger a full qmd update+embed (facts are already on disk)."""
        await self._run_update()


class HybridSearchBackend(SearchBackend):
    """Hybrid search combining QMD semantic search and keyword search.

    Runs both backends in parallel for every query and merges results using
    Reciprocal Rank Fusion (RRF).  Facts that appear in both ranked lists
    receive a consensus boost; unique hits from either list are still included.
    After RRF, vault quality signals (confidence, tier, reinforcement) are
    applied as a final re-rank multiplier.

    index_fact / remove_fact are delegated to both backends.

    Args:
        qmd: A QmdSearchBackend instance.
        keyword: A FallbackSearchBackend instance.
        rrf_k: RRF smoothing constant (default 60, standard value).
    """

    def __init__(
        self,
        qmd: QmdSearchBackend,
        keyword: FallbackSearchBackend,
        rrf_k: int = 60,
    ) -> None:
        self._qmd = qmd
        self._keyword = keyword
        self._rrf_k = rrf_k

    async def index_fact(self, fact: VaultFact) -> None:
        import asyncio
        await asyncio.gather(
            self._qmd.index_fact(fact),
            self._keyword.index_fact(fact),
        )

    async def remove_fact(self, fact_id: str) -> None:
        import asyncio
        await asyncio.gather(
            self._qmd.remove_fact(fact_id),
            self._keyword.remove_fact(fact_id),
        )

    async def search(
        self,
        query: str,
        limit: int = 20,
        filters: Optional[dict] = None,
    ) -> list[SearchResult]:
        """Run QMD and keyword search in parallel, merge via RRF."""
        import asyncio

        if not query.strip():
            return []

        qmd_results, kw_results = await asyncio.gather(
            self._qmd.search(query, limit=limit, filters=filters),
            self._keyword.search(query, limit=limit, filters=filters),
        )

        # Build rank maps: fact_id -> 1-based rank
        qmd_ranks: dict[str, int] = {r.fact_id: i + 1 for i, r in enumerate(qmd_results)}
        kw_ranks: dict[str, int] = {r.fact_id: i + 1 for i, r in enumerate(kw_results)}

        # Collect all unique fact IDs seen by either backend
        all_ids: set[str] = set(qmd_ranks) | set(kw_ranks)

        # Build a combined fact lookup from both result lists
        fact_lookup: dict[str, Optional[VaultFact]] = {}
        for r in qmd_results + kw_results:
            if r.fact_id not in fact_lookup or fact_lookup[r.fact_id] is None:
                fact_lookup[r.fact_id] = r.fact

        k = self._rrf_k
        # Theoretical ceiling: rank-1 in both backends × max quality multipliers
        # (1 + 0.5 confidence) × (1 + 0.5 reinforcement cap) = 1.5 × 1.5 = 2.25
        _rrf_ceil = (2.0 / (k + 1)) * 2.25

        merged: list[SearchResult] = []
        for fact_id in all_ids:
            rrf = 0.0
            if fact_id in qmd_ranks:
                rrf += 1.0 / (k + qmd_ranks[fact_id])
            if fact_id in kw_ranks:
                rrf += 1.0 / (k + kw_ranks[fact_id])

            # Apply vault quality boost
            fact = fact_lookup.get(fact_id)
            if fact is not None:
                rrf *= 1.0 + fact.confidence * 0.5
                rrf *= 1.0 + min(fact.reinforcement_count * 0.1, 0.5)
                tier_penalty = (fact.tier - 1) * 0.05
                rrf *= max(1.0 - tier_penalty, 0.5)

            # Normalize to [0, 1] against theoretical ceiling so min_relevance_score
            # is meaningful regardless of RRF constant or backend configuration.
            normalized = min(rrf / _rrf_ceil, 1.0)
            merged.append(SearchResult(fact_id=fact_id, score=normalized, fact=fact))

        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:limit]

    async def reindex_all(self, facts: list[VaultFact]) -> None:
        import asyncio
        await asyncio.gather(
            self._qmd.reindex_all(facts),
            self._keyword.reindex_all(facts),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _qmd_available(qmd_path: str = "qmd") -> bool:
    """Return True if the qmd binary is on PATH (or at the given path)."""
    return shutil.which(qmd_path) is not None


def create_search_backend(
    store,
    collection: str = "",
    qmd_path: str = "qmd",
) -> SearchBackend:
    """Create the best available search backend for a vault store.

    Auto-detects whether the qmd binary is available:
    - qmd found  → HybridSearchBackend (QMD semantic + keyword, merged via RRF)
    - qmd absent → FallbackSearchBackend (keyword-only, no external dependencies)

    Args:
        store: VaultStore instance.
        collection: QMD collection name (e.g. ``"niggy-vault"``).
            Only used when qmd is available.
        qmd_path: Path to the qmd binary. Defaults to ``"qmd"`` (on PATH).

    Returns:
        A SearchBackend instance ready for use.
    """
    keyword = FallbackSearchBackend(store)
    if _qmd_available(qmd_path):
        qmd = QmdSearchBackend(collection=collection, store=store, qmd_path=qmd_path)
        return HybridSearchBackend(qmd=qmd, keyword=keyword)
    return keyword
