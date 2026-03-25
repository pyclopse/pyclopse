"""VaultStore — reads and writes VaultFact as markdown files with YAML frontmatter.

File layout:
    {vault_dir}/facts/{ulid}.md     — active facts
    {vault_dir}/archive/{ulid}.md   — superseded/archived facts

File format::

    ---
    id: 01ABC...
    type: preference
    state: crystallized
    claim: "User prefers Python"
    confidence: 0.85
    ...all other frontmatter fields...
    ---

    Narrative body text here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

from .models import (
    SourceSession,
    VaultFact,
    VaultFactState,
)

logger = logging.getLogger("pyclawops.vault.store")

_yaml = YAML()
_yaml.default_flow_style = False
_yaml.preserve_quotes = True


def _serialize_fact_frontmatter(fact: VaultFact) -> dict:
    """Convert a VaultFact to a plain dict suitable for YAML serialization."""
    data: dict = {
        "id": fact.id,
        "type": fact.type,
        "state": fact.state.value if isinstance(fact.state, VaultFactState) else fact.state,
        "claim": fact.claim,
        "confidence": fact.confidence,
        "reinforcement_count": fact.reinforcement_count,
        "surprise_score": fact.surprise_score,
        "implied": fact.implied,
        "tier": fact.tier,
    }
    # Optional string fields
    if fact.contrastive is not None:
        data["contrastive"] = fact.contrastive
    if fact.source_file is not None:
        data["source_file"] = fact.source_file
    if fact.supersedes is not None:
        data["supersedes"] = fact.supersedes
    if fact.superseded_by is not None:
        data["superseded_by"] = fact.superseded_by

    # Datetime fields — always store as ISO 8601 strings
    def _dt(val: Optional[datetime]) -> Optional[str]:
        return val.isoformat() if val is not None else None

    data["written_at"] = _dt(fact.written_at)
    data["event_at"] = _dt(fact.event_at)
    data["stated_at"] = _dt(fact.stated_at)
    data["expires_at"] = _dt(fact.expires_at)
    data["valid_from"] = _dt(fact.valid_from)
    data["valid_until"] = _dt(fact.valid_until)

    # List / typed-link fields
    data["related_to"] = list(fact.related_to)
    data["depends_on"] = list(fact.depends_on)
    if fact.part_of is not None:
        data["part_of"] = fact.part_of
    data["contradicts"] = list(fact.contradicts)
    data["source_sessions"] = [
        {"session_id": s.session_id, "message_range": list(s.message_range)}
        for s in fact.source_sessions
    ]

    return data


def _parse_fact_file(path: Path) -> Optional[VaultFact]:
    """Parse a vault fact file (frontmatter + body) into a VaultFact.

    Returns None if parsing fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Split on --- delimiters
    # Format: "---\n<yaml>\n---\n<body>"
    if not text.startswith("---"):
        logger.warning("Vault file missing frontmatter delimiter: %s", path)
        return None

    # Find the closing ---
    rest = text[3:]  # strip leading ---
    if rest.startswith("\n"):
        rest = rest[1:]

    end_idx = rest.find("\n---")
    if end_idx == -1:
        logger.warning("Vault file missing closing frontmatter delimiter: %s", path)
        return None

    yaml_text = rest[:end_idx]
    body_text = rest[end_idx + 4:].lstrip("\n")  # skip past "\n---"

    try:
        data = _yaml.load(StringIO(yaml_text))
    except Exception as exc:
        logger.warning("Failed to parse YAML in %s: %s", path, exc)
        return None

    if not isinstance(data, dict):
        return None

    def _dt(val) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
        try:
            dt = datetime.fromisoformat(str(val))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            return None

    source_sessions = []
    for s in data.get("source_sessions") or []:
        if isinstance(s, dict):
            mr = s.get("message_range", [0, 0])
            source_sessions.append(
                SourceSession(session_id=str(s.get("session_id", "")), message_range=tuple(mr))
            )

    try:
        fact = VaultFact(
            id=str(data.get("id", "")),
            type=str(data.get("type", "fact")),
            state=VaultFactState(data.get("state", "provisional")),
            claim=str(data.get("claim", "")),
            contrastive=data.get("contrastive"),
            implied=bool(data.get("implied", False)),
            confidence=float(data.get("confidence", 0.7)),
            reinforcement_count=int(data.get("reinforcement_count", 0)),
            surprise_score=float(data.get("surprise_score", 0.0)),
            event_at=_dt(data.get("event_at")),
            stated_at=_dt(data.get("stated_at")),
            written_at=_dt(data.get("written_at")) or datetime.now(timezone.utc),
            expires_at=_dt(data.get("expires_at")),
            valid_from=_dt(data.get("valid_from")),
            valid_until=_dt(data.get("valid_until")),
            source_sessions=source_sessions,
            source_file=data.get("source_file"),
            supersedes=data.get("supersedes"),
            superseded_by=data.get("superseded_by"),
            related_to=list(data.get("related_to") or []),
            depends_on=list(data.get("depends_on") or []),
            part_of=data.get("part_of"),
            contradicts=list(data.get("contradicts") or []),
            tier=int(data.get("tier", 1)),
            body=body_text,
        )
    except Exception as exc:
        logger.warning("Failed to construct VaultFact from %s: %s", path, exc)
        return None

    return fact


def _write_fact_file(path: Path, fact: VaultFact) -> None:
    """Atomically write a VaultFact to a markdown file with YAML frontmatter."""
    frontmatter = _serialize_fact_frontmatter(fact)

    buf = StringIO()
    _yaml.dump(frontmatter, buf)
    yaml_text = buf.getvalue()

    content = f"---\n{yaml_text}---\n\n{fact.body}"

    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class VaultStore:
    """Reads and writes VaultFact as markdown files with YAML frontmatter."""

    def __init__(self, vault_dir: Path) -> None:
        self._vault_dir = vault_dir
        self._facts_dir = vault_dir / "facts"
        self._archive_dir = vault_dir / "archive"
        self._facts_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    @property
    def facts_dir(self) -> Path:
        return self._facts_dir

    def _fact_path(self, fact_id: str) -> Path:
        return self._facts_dir / f"{fact_id}.md"

    def _archive_path(self, fact_id: str) -> Path:
        return self._archive_dir / f"{fact_id}.md"

    def write_fact(self, fact: VaultFact) -> None:
        """Write fact to disk. Atomic write (tmp + rename).

        If the fact is superseded or archived, writes to archive/; otherwise facts/.
        """
        if fact.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
            path = self._archive_path(fact.id)
        else:
            path = self._fact_path(fact.id)
        _write_fact_file(path, fact)

    def read_fact(self, fact_id: str) -> Optional[VaultFact]:
        """Read fact by ULID. Checks facts/ then archive/.

        Returns None if not found.
        """
        for path in (self._fact_path(fact_id), self._archive_path(fact_id)):
            if path.exists():
                return _parse_fact_file(path)
        return None

    def list_facts(
        self,
        states: Optional[set[VaultFactState]] = None,
        types: Optional[set[str]] = None,
        min_confidence: float = 0.0,
        valid_at: Optional[datetime] = None,
        tier_max: Optional[int] = None,
        source_file: Optional[str] = None,
        include_archive: bool = False,
    ) -> list[VaultFact]:
        """List facts with optional filtering.

        Args:
            states: Only return facts with one of these states. None = all active states.
            types: Only return facts with one of these type strings. None = all types.
            min_confidence: Minimum confidence threshold (inclusive).
            valid_at: Only return facts valid at this datetime (valid_until is None or in future).
            tier_max: Only return facts with tier <= tier_max.
            source_file: Only return facts with this source_file value.
            include_archive: If True, also scan archive/ directory.
        """
        dirs = [self._facts_dir]
        if include_archive:
            dirs.append(self._archive_dir)

        results: list[VaultFact] = []
        for directory in dirs:
            for md_file in directory.glob("*.md"):
                fact = _parse_fact_file(md_file)
                if fact is None:
                    continue

                # State filter
                if states is not None and fact.state not in states:
                    continue

                # Type filter
                if types is not None and fact.type not in types:
                    continue

                # Confidence filter
                if fact.confidence < min_confidence:
                    continue

                # Tier filter
                if tier_max is not None and fact.tier > tier_max:
                    continue

                # Source file filter
                if source_file is not None and fact.source_file != source_file:
                    continue

                # Temporal validity filter
                if valid_at is not None:
                    if fact.valid_from is not None and fact.valid_from > valid_at:
                        continue
                    if fact.valid_until is not None and fact.valid_until <= valid_at:
                        continue

                results.append(fact)

        return results

    def update_fact(self, fact_id: str, **updates) -> VaultFact:
        """Update fields on an existing fact. Returns updated fact.

        Raises FileNotFoundError if fact_id is not found.
        """
        fact = self.read_fact(fact_id)
        if fact is None:
            raise FileNotFoundError(f"Fact not found: {fact_id}")

        old_state = fact.state
        updated_data = fact.model_dump()
        updated_data.update(updates)
        new_fact = VaultFact(**updated_data)

        # If state changed from active to archived/superseded, clean up old location
        active_states = {VaultFactState.PROVISIONAL, VaultFactState.CRYSTALLIZED}
        inactive_states = {VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED}

        if old_state in active_states and new_fact.state in inactive_states:
            # Remove from facts/, write to archive/
            old_path = self._fact_path(fact_id)
            if old_path.exists():
                old_path.unlink()

        self.write_fact(new_fact)
        return new_fact

    def supersede_fact(self, old_id: str, new_fact: VaultFact) -> tuple[VaultFact, VaultFact]:
        """Mark old_id as superseded by new_fact. Writes both.

        Returns (old_fact, new_fact).
        Raises FileNotFoundError if old_id is not found.
        """
        old_fact = self.read_fact(old_id)
        if old_fact is None:
            raise FileNotFoundError(f"Fact not found: {old_id}")

        now = datetime.now(timezone.utc)

        # Update old fact
        updated_old = old_fact.model_copy(update={
            "state": VaultFactState.SUPERSEDED,
            "superseded_by": new_fact.id,
            "valid_until": now,
        })
        # Move old fact to archive (remove from facts/)
        old_path = self._fact_path(old_id)
        if old_path.exists():
            old_path.unlink()
        _write_fact_file(self._archive_path(old_id), updated_old)

        # Set new fact's supersedes field and write it
        updated_new = new_fact.model_copy(update={"supersedes": old_id})
        self.write_fact(updated_new)

        return updated_old, updated_new

    def reinforce_fact(self, fact_id: str, session: SourceSession) -> VaultFact:
        """Increment reinforcement_count, add source session.

        Returns updated fact.
        Raises FileNotFoundError if fact_id is not found.
        """
        fact = self.read_fact(fact_id)
        if fact is None:
            raise FileNotFoundError(f"Fact not found: {fact_id}")

        new_sessions = list(fact.source_sessions) + [session]
        updated = fact.model_copy(update={
            "reinforcement_count": fact.reinforcement_count + 1,
            "source_sessions": new_sessions,
        })
        self.write_fact(updated)
        return updated

    def archive_fact(self, fact_id: str, reason: str = "") -> VaultFact:
        """Move fact to archive/ directory, set state=archived.

        Returns updated fact.
        Raises FileNotFoundError if fact_id is not found.
        """
        fact = self.read_fact(fact_id)
        if fact is None:
            raise FileNotFoundError(f"Fact not found: {fact_id}")

        updated = fact.model_copy(update={"state": VaultFactState.ARCHIVED})
        if reason and not updated.body:
            updated = updated.model_copy(update={"body": f"Archived: {reason}"})
        elif reason:
            updated = updated.model_copy(update={"body": updated.body + f"\n\nArchived: {reason}"})

        # Remove from facts/ if present
        active_path = self._fact_path(fact_id)
        if active_path.exists():
            active_path.unlink()

        _write_fact_file(self._archive_path(fact_id), updated)
        return updated

    def delete_facts_by_source_file(self, file_path: str) -> list[str]:
        """Archive all facts where source_file == file_path.

        Returns list of archived fact IDs.
        """
        facts = self.list_facts(source_file=file_path)
        archived_ids = []
        for fact in facts:
            self.archive_fact(fact.id, reason=f"source_file_deleted: {file_path}")
            archived_ids.append(fact.id)
        return archived_ids

    def get_stats(self) -> dict:
        """Return counts by state, type, and tier."""
        all_facts = self.list_facts(include_archive=True)

        by_state: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_tier: dict[int, int] = {}

        for fact in all_facts:
            state_key = fact.state.value if isinstance(fact.state, VaultFactState) else str(fact.state)
            by_state[state_key] = by_state.get(state_key, 0) + 1
            by_type[fact.type] = by_type.get(fact.type, 0) + 1
            by_tier[fact.tier] = by_tier.get(fact.tier, 0) + 1

        return {
            "total": len(all_facts),
            "by_state": by_state,
            "by_type": by_type,
            "by_tier": by_tier,
        }
