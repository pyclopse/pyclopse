"""Wikilink parsing and resolution for vault facts.

Supports two syntaxes in fact bodies:
  [[target]]             — generic similarity link (→ related_to)
  [[type::target]]       — typed link, where type is one of:
                              depends_on, part_of, contradicts

Pipe display text is also supported:
  [[target|display]]
  [[type::target|display]]

Targets are resolved to vault fact IDs at write time and stored in the
appropriate VaultFact field so the ContextAssembler can follow them
during retrieval (graph traversal).

Resolution order:
1. Direct ULID ID match  (e.g. [[01KMG44QGBDXWQVXRB24Y2X459]])
2. Exact case-insensitive claim match
3. Substring match — target text appears anywhere in a fact's claim
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import VaultFact

logger = logging.getLogger("pyclaw.vault.links")

# Matches [[anything]] — typed and untyped, with optional pipe display text
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Valid typed link prefixes
VALID_LINK_TYPES = frozenset({"depends_on", "part_of", "contradicts", "related_to"})


def _parse_raw_link(raw: str) -> tuple[Optional[str], str]:
    """Parse a raw wikilink capture group into (link_type, target).

    Handles:
      ``target``                  → (None, "target")
      ``type::target``            → ("type", "target")
      ``target|display``          → (None, "target")
      ``type::target|display``    → ("type", "target")

    An unknown type prefix is logged and falls back to None (→ related_to).
    """
    # Strip display text first
    inner = raw.strip()
    if "|" in inner:
        inner = inner.split("|", 1)[0].strip()

    # Check for type prefix
    if "::" in inner:
        link_type, target = inner.split("::", 1)
        link_type = link_type.strip().lower()
        target = target.strip()
        if link_type not in VALID_LINK_TYPES:
            logger.debug("Unknown wikilink type %r, treating as related_to", link_type)
            link_type = None
        return link_type, target

    return None, inner


def parse_wikilinks(text: Optional[str]) -> list[str]:
    """Extract untyped wikilink targets from text (backward-compatible).

    Strips display text after pipe: ``[[foo|bar]]`` → ``"foo"``.
    Strips type prefix: ``[[depends_on::foo]]`` → ``"foo"``.
    Returns normalised, deduplicated targets preserving order.
    """
    if not text:
        return []
    seen: set[str] = set()
    targets: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        _, target = _parse_raw_link(m.group(1))
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def parse_typed_wikilinks(text: Optional[str]) -> list[tuple[Optional[str], str]]:
    """Extract all wikilinks from text as (link_type, target) tuples.

    ``link_type`` is one of the VALID_LINK_TYPES or None (meaning related_to).
    Deduplicated by (type, target) pair, order preserved.
    """
    if not text:
        return []
    seen: set[tuple[Optional[str], str]] = set()
    results: list[tuple[Optional[str], str]] = []
    for m in _WIKILINK_RE.finditer(text):
        link_type, target = _parse_raw_link(m.group(1))
        if target:
            key = (link_type, target)
            if key not in seen:
                seen.add(key)
                results.append(key)
    return results


def resolve_wikilink(target: str, facts: list["VaultFact"]) -> Optional[str]:
    """Resolve a wikilink target to a fact ID.

    Args:
        target: Raw wikilink target string.
        facts: List of facts to search against.

    Returns:
        Matching fact ID, or None if unresolvable.
    """
    if not target or not facts:
        return None

    target_upper = target.strip().upper()
    target_lower = target.strip().lower()

    # 1. Direct ULID ID match
    for f in facts:
        if f.id == target_upper:
            return f.id

    # 2. Exact claim match (case-insensitive)
    for f in facts:
        if f.claim.lower() == target_lower:
            return f.id

    # 3. Substring — target appears in claim
    for f in facts:
        if target_lower in f.claim.lower():
            return f.id

    return None


def resolve_fact_links(fact: "VaultFact", all_facts: list["VaultFact"]) -> list[str]:
    """Parse untyped wikilinks from a fact's body, resolve to IDs → related_to.

    Backward-compatible: only returns generic related_to IDs.
    For typed links use resolve_fact_typed_links.

    Args:
        fact: The fact whose links to resolve.
        all_facts: Full list of facts in the vault (excluding ``fact`` itself).

    Returns:
        Deduplicated list of resolved fact IDs for related_to.
    """
    others = [f for f in all_facts if f.id != fact.id]
    # Only process untyped links here
    typed_pairs = parse_typed_wikilinks(fact.body)
    untyped_targets = [t for lt, t in typed_pairs if lt is None]

    resolved: list[str] = []
    seen: set[str] = set()
    for target in untyped_targets:
        fact_id = resolve_wikilink(target, others)
        if fact_id and fact_id not in seen:
            seen.add(fact_id)
            resolved.append(fact_id)
    return resolved


def resolve_fact_typed_links(
    fact: "VaultFact",
    all_facts: list["VaultFact"],
) -> dict[str, list[str]]:
    """Parse all wikilinks from a fact's body, resolve to IDs grouped by link type.

    Returns a dict with keys matching VALID_LINK_TYPES (minus "related_to" which
    goes under "related_to"). Unresolvable targets are silently dropped.

    Args:
        fact: The fact whose body to parse.
        all_facts: Full list of facts in the vault (excluding ``fact`` itself).

    Returns:
        Dict mapping link type → list of resolved fact IDs.
        Example: {"related_to": [...], "depends_on": [...], "contradicts": [...]}
    """
    others = [f for f in all_facts if f.id != fact.id]
    typed_pairs = parse_typed_wikilinks(fact.body)

    result: dict[str, list[str]] = {lt: [] for lt in VALID_LINK_TYPES}
    seen: dict[str, set[str]] = {lt: set() for lt in VALID_LINK_TYPES}

    for link_type, target in typed_pairs:
        bucket = link_type if link_type in VALID_LINK_TYPES else "related_to"
        fact_id = resolve_wikilink(target, others)
        if fact_id and fact_id not in seen[bucket]:
            seen[bucket].add(fact_id)
            result[bucket].append(fact_id)

    return result


def strip_wikilinks(text: Optional[str]) -> str:
    """Return text with wikilink syntax removed (keep display text or target).

    Handles typed links: ``[[depends_on::foo]]`` → ``"foo"``.
    Handles pipe display: ``[[foo|bar]]`` → ``"bar"``.
    """
    if not text:
        return text or ""

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        # Strip pipe display text
        if "|" in inner:
            return inner.split("|", 1)[1].strip()
        # Strip type prefix
        if "::" in inner:
            return inner.split("::", 1)[1].strip()
        return inner.strip()

    return _WIKILINK_RE.sub(_replace, text)
