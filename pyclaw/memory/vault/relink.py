"""Retroactive wikilink migration utility for vault facts.

Scans all active facts in an agent's vault, builds a similarity-based
graph, and populates the ``related_to`` field on each fact.

Unlike write-time resolution (which only resolves ``[[wikilink]]`` syntax
in the fact body), this utility uses keyword search to infer relationships
between facts that were created before wikilink support existed.

Usage::

    uv run python -m pyclaw.memory.vault.relink <agent_name> [options]

    # Dry run (show what would change, no writes)
    uv run python -m pyclaw.memory.vault.relink niggy --dry-run

    # Set custom similarity threshold (default 3.0)
    uv run python -m pyclaw.memory.vault.relink niggy --threshold 2.0

    # Multiple agents
    uv run python -m pyclaw.memory.vault.relink niggy ritchie via

    # Process all agents found under ~/.pyclaw/agents/
    uv run python -m pyclaw.memory.vault.relink --all

Note: always uses fast in-memory keyword search regardless of whether QMD
is installed — running hundreds of subprocess calls would be too slow.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Optional

from .links import resolve_fact_links
from .models import VaultFact, VaultFactState
from .search import FallbackSearchBackend, HybridSearchBackend, SearchBackend, create_search_backend
from .store import VaultStore

logger = logging.getLogger("pyclaw.vault.relink")


def _get_agents_dir() -> Path:
    return Path.home() / ".pyclaw" / "agents"


def _get_vault_dir(agent_name: str) -> Path:
    return _get_agents_dir() / agent_name / "vault"


def _load_all_active_facts(store: VaultStore) -> list[VaultFact]:
    """Load all non-archived, non-superseded facts from the store."""
    all_facts = store.list_facts()
    return [
        f for f in all_facts
        if f.state not in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED)
    ]


def _claim_overlap(a: str, b: str) -> float:
    """Return word-level Jaccard similarity between two claim strings.

    Returns a value in [0.0, 1.0].  High values mean the claims are likely
    duplicate/near-duplicate facts and should NOT be linked.
    """
    words_a = set(re.sub(r"[^\w\s]", "", a.lower()).split())
    words_b = set(re.sub(r"[^\w\s]", "", b.lower()).split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


async def _build_similarity_links(
    fact: VaultFact,
    all_facts: list[VaultFact],
    search: SearchBackend,
    threshold: float,
    max_links: int,
    max_overlap: float = 0.6,
    fact_map: Optional[dict[str, VaultFact]] = None,
) -> list[str]:
    """Find related facts via search on the fact's claim.

    Args:
        fact: The fact to find links for.
        all_facts: All active facts in the vault.
        search: Search backend to use.
        threshold: Minimum score to consider a fact related.
            For FallbackSearchBackend scores are in the 1–20 range.
            For HybridSearchBackend RRF scores max at ~0.07, so the threshold
            is ignored and top-N results are taken instead (ranking is
            already quality-weighted by RRF + vault boosts).
        max_links: Maximum number of links to add per fact.
        max_overlap: Maximum Jaccard claim overlap to accept.  Facts with
            higher overlap are likely near-duplicates and skipped (default 0.6).
        fact_map: Optional pre-built {id: fact} lookup for overlap checks.

    Returns:
        List of fact IDs that should be in related_to.
    """
    # Use the claim as the search query
    query = fact.claim
    if fact.body:
        # Append first 100 chars of body for richer signal
        query = f"{query} {fact.body[:100]}"

    results = await search.search(query, limit=max_links * 3)

    # For hybrid/RRF backends the absolute score is not comparable to the
    # keyword threshold — just take top-N by rank (score already incorporates
    # quality signals).
    use_threshold = not isinstance(search, HybridSearchBackend)

    linked: list[str] = []
    seen: set[str] = {fact.id}
    for result in results:
        if result.fact_id in seen:
            continue
        if use_threshold and result.score < threshold:
            continue
        # Skip near-duplicate claims (high word overlap = probably same fact
        # rephrased slightly, not a meaningful cross-concept relationship)
        if fact_map and result.fact_id in fact_map:
            overlap = _claim_overlap(fact.claim, fact_map[result.fact_id].claim)
            if overlap >= max_overlap:
                continue
        if len(linked) >= max_links:
            break
        seen.add(result.fact_id)
        linked.append(result.fact_id)

    return linked


def _merge_links(existing: list[str], new_links: list[str]) -> list[str]:
    """Merge new links into existing, preserving order, deduplicating."""
    seen = set(existing)
    merged = list(existing)
    for link_id in new_links:
        if link_id not in seen:
            seen.add(link_id)
            merged.append(link_id)
    return merged


async def relink_agent(
    agent_name: str,
    threshold: float = 3.0,
    max_links: int = 5,
    max_overlap: float = 0.6,
    dry_run: bool = False,
    qmd_collection: str = "",
) -> dict:
    """Run retroactive relinking for a single agent.

    Args:
        agent_name: Name of the agent (used to locate vault dir).
        threshold: Minimum similarity score to create a link.
        max_links: Maximum related_to entries to add per fact.
        dry_run: If True, report changes but do not write to disk.
        qmd_collection: Optional QMD collection name for hybrid search.

    Returns:
        Stats dict: facts_scanned, facts_updated, links_added, errors.
    """
    vault_dir = _get_vault_dir(agent_name)
    if not vault_dir.exists():
        print(f"  [SKIP] No vault found at {vault_dir}", file=sys.stderr)
        return {"facts_scanned": 0, "facts_updated": 0, "links_added": 0, "errors": 0}

    stats = {"facts_scanned": 0, "facts_updated": 0, "links_added": 0, "errors": 0}

    store = VaultStore(vault_dir)
    facts = _load_all_active_facts(store)

    if not facts:
        print(f"  [SKIP] No active facts in {vault_dir}")
        return stats

    stats["facts_scanned"] = len(facts)
    print(f"  Loaded {len(facts)} active facts")

    # Use hybrid/QMD search when a collection is specified, keyword-only otherwise.
    # QMD gives better semantic matching; keyword is the no-dependency fallback.
    if qmd_collection:
        search = create_search_backend(store, collection=qmd_collection)
        print(f"  Search: hybrid (QMD collection={qmd_collection!r} + keyword)")
    else:
        search = FallbackSearchBackend(store)
        print("  Search: keyword-only (pass --qmd-collection to use semantic search)")

    # Index all facts into the search backend
    print("  Indexing facts into search backend...")
    for fact in facts:
        try:
            await search.index_fact(fact)
        except Exception as exc:
            logger.debug("Failed to index fact %s: %s", fact.id, exc)

    # Phase 1: resolve explicit [[wikilinks]] from claims/bodies
    print("  Phase 1: resolving explicit [[wikilinks]]...")
    wikilink_updates: dict[str, list[str]] = {}
    for fact in facts:
        try:
            resolved = resolve_fact_links(fact, facts)
            if resolved:
                new_related = _merge_links(list(fact.related_to), resolved)
                if new_related != list(fact.related_to):
                    wikilink_updates[fact.id] = new_related
        except Exception as exc:
            logger.warning("Error resolving wikilinks for %s: %s", fact.id, exc)
            stats["errors"] += 1

    print(f"    {len(wikilink_updates)} facts have wikilink updates")

    # Phase 2: similarity-based relinking via search
    fact_map = {f.id: f for f in facts}
    print(f"  Phase 2: similarity search (threshold={threshold}, max_overlap={max_overlap})...")
    similarity_updates: dict[str, list[str]] = {}
    for i, fact in enumerate(facts):
        try:
            linked_ids = await _build_similarity_links(
                fact, facts, search, threshold, max_links,
                max_overlap=max_overlap,
                fact_map=fact_map,
            )
            if linked_ids:
                # Start from wikilink updates if any, else current related_to
                base = wikilink_updates.get(fact.id, list(fact.related_to))
                new_related = _merge_links(base, linked_ids)
                if new_related != list(fact.related_to):
                    similarity_updates[fact.id] = new_related

            if (i + 1) % 20 == 0:
                print(f"    ... processed {i + 1}/{len(facts)}")
        except Exception as exc:
            logger.warning("Error finding links for %s: %s", fact.id, exc)
            stats["errors"] += 1

    # Merge both phases
    all_updates: dict[str, list[str]] = {}
    all_ids = set(wikilink_updates) | set(similarity_updates)
    for fact_id in all_ids:
        base = wikilink_updates.get(fact_id, None)
        sim = similarity_updates.get(fact_id, None)
        if base is not None and sim is not None:
            all_updates[fact_id] = sim  # sim already merged from wikilink base
        elif base is not None:
            all_updates[fact_id] = base
        else:
            all_updates[fact_id] = sim  # type: ignore[assignment]

    print(f"  {len(all_updates)} facts will be updated")

    if dry_run:
        print("  [DRY RUN] Changes (not written):")
        fact_map = {f.id: f for f in facts}
        for fact_id, new_related in all_updates.items():
            fact = fact_map.get(fact_id)
            if fact is None:
                continue
            old = list(fact.related_to)
            added = [r for r in new_related if r not in old]
            removed = [r for r in old if r not in new_related]
            print(f"    {fact_id[:10]}... ({fact.claim[:50]})")
            for link_id in added:
                linked_fact = fact_map.get(link_id)
                claim = linked_fact.claim[:50] if linked_fact else "?"
                print(f"      + {link_id[:10]}  \"{claim}\"")
            for link_id in removed:
                print(f"      - {link_id}")
            stats["facts_updated"] += 1
            stats["links_added"] += len(added)
        return stats

    # Write updates
    fact_map = {f.id: f for f in facts}
    for fact_id, new_related in all_updates.items():
        fact = fact_map.get(fact_id)
        if fact is None:
            continue
        old = list(fact.related_to)
        added = [r for r in new_related if r not in old]
        try:
            updated_fact = fact.model_copy(update={"related_to": new_related})
            store.write_fact(updated_fact)
            stats["facts_updated"] += 1
            stats["links_added"] += len(added)
            logger.debug(
                "Updated %s: +%d links -> %s",
                fact_id,
                len(added),
                added,
            )
        except Exception as exc:
            logger.warning("Failed to write updated fact %s: %s", fact_id, exc)
            stats["errors"] += 1

    return stats


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Determine agent list
    if args.all:
        agents_dir = _get_agents_dir()
        if not agents_dir.exists():
            print(f"No agents directory found at {agents_dir}", file=sys.stderr)
            return 1
        agent_names = [
            d.name for d in agents_dir.iterdir()
            if d.is_dir() and (d / "vault").exists()
        ]
        if not agent_names:
            print("No agents with vaults found.", file=sys.stderr)
            return 1
    else:
        agent_names = args.agents

    if not agent_names:
        print("No agents specified. Use --all or provide agent names.", file=sys.stderr)
        return 1

    total_stats: dict = {
        "facts_scanned": 0,
        "facts_updated": 0,
        "links_added": 0,
        "errors": 0,
    }

    for agent_name in agent_names:
        print(f"\nProcessing agent: {agent_name}")
        stats = await relink_agent(
            agent_name=agent_name,
            threshold=args.threshold,
            max_links=args.max_links,
            max_overlap=args.max_overlap,
            dry_run=args.dry_run,
            qmd_collection=args.qmd_collection or "",
        )
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)
        print(
            f"  Done: {stats['facts_scanned']} scanned, "
            f"{stats['facts_updated']} updated, "
            f"{stats['links_added']} links added, "
            f"{stats['errors']} errors"
        )

    if len(agent_names) > 1:
        print(
            f"\nTotal: {total_stats['facts_scanned']} scanned, "
            f"{total_stats['facts_updated']} updated, "
            f"{total_stats['links_added']} links added, "
            f"{total_stats['errors']} errors"
        )

    if args.dry_run:
        print("\n[DRY RUN] No changes were written.")

    return 0 if total_stats["errors"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactively add wikilinks between related vault facts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "agents",
        nargs="*",
        metavar="AGENT",
        help="Agent name(s) to process (e.g. niggy ritchie)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all agents that have a vault directory",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=3.0,
        help="Minimum similarity score to create a link (default: 3.0)",
    )
    parser.add_argument(
        "--max-links",
        type=int,
        default=5,
        dest="max_links",
        help="Maximum links to add per fact (default: 5)",
    )
    parser.add_argument(
        "--max-overlap",
        type=float,
        default=0.6,
        dest="max_overlap",
        metavar="RATIO",
        help="Skip linking facts whose claims share more than this fraction "
             "of words (Jaccard). Prevents near-duplicate facts from linking "
             "to each other (default: 0.6)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to disk",
    )
    parser.add_argument(
        "--qmd-collection",
        default="",
        dest="qmd_collection",
        metavar="COLLECTION",
        help="QMD collection name for semantic search (e.g. niggy-vault). "
             "When provided, uses hybrid QMD+keyword search for better results. "
             "Without this, falls back to keyword-only search.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
