"""IngestionHandler — orchestrates memory ingestion from conversations and documents.

The handler sits between the cursor store, memory agent, and vault store.
It coordinates the full pipeline: detect new content → call agent → persist facts.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timezone, datetime
from pathlib import Path
from typing import Optional

from .agent import MemoryAgent
from .cursor import CursorStore
from .models import (
    ExtractionAction,
    ExtractionResult,
    FactExtraction,
    SourceSession,
    VaultFact,
    VaultFactState,
)
from .registry import TypeSchemaRegistry
from .search import SearchBackend
from .store import VaultStore
from . import ulid as _ulid

logger = logging.getLogger("pyclopse.vault.ingestion")

_DEFAULT_SKIP_CHANNELS = {"job", "a2a"}


def _hash_content(content: str) -> str:
    """Return SHA-256 hex digest of content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_fact_from_extraction(extraction: FactExtraction, session_id: str, message_range: tuple[int, int]) -> VaultFact:
    """Build a VaultFact from a FactExtraction's fact_fields dict."""
    fields = dict(extraction.fact_fields)

    # Ensure required defaults
    fields.setdefault("id", _ulid.generate())
    fields.setdefault("state", VaultFactState.PROVISIONAL)

    # Add source session provenance
    source_session = SourceSession(session_id=session_id, message_range=message_range)
    existing_sessions = fields.pop("source_sessions", [])
    if isinstance(existing_sessions, list):
        fields["source_sessions"] = existing_sessions + [source_session]
    else:
        fields["source_sessions"] = [source_session]

    # written_at defaults to now
    fields.setdefault("written_at", datetime.now(timezone.utc))

    return VaultFact(**fields)


def _build_fact_from_document_extraction(
    extraction: FactExtraction, document_path: str
) -> VaultFact:
    """Build a VaultFact from a document FactExtraction."""
    fields = dict(extraction.fact_fields)
    fields.setdefault("id", _ulid.generate())
    fields.setdefault("state", VaultFactState.PROVISIONAL)
    fields.setdefault("source_file", document_path)
    fields.setdefault("written_at", datetime.now(timezone.utc))
    return VaultFact(**fields)


class IngestionHandler:
    """Orchestrates memory ingestion from conversations and documents."""

    def __init__(
        self,
        vault_dir: Path,
        store: VaultStore,
        cursor: CursorStore,
        search: SearchBackend,
        registry: TypeSchemaRegistry,
        agent: MemoryAgent,
    ) -> None:
        self._vault_dir = vault_dir
        self._store = store
        self._cursor = cursor
        self._search = search
        self._registry = registry
        self._agent = agent

    async def ingest_conversation_turn(
        self,
        session_id: str,
        messages: list[dict],
        message_range: tuple[int, int],
        channel: str,
    ) -> ExtractionResult:
        """Ingest a conversation segment and extract/update vault facts.

        Steps:
        1. Set currently_processing in cursor store (crash recovery)
        2. Load top related existing facts (search by message content)
        3. Call memory agent
        4. Process extractions: create / reinforce / supersede
        5. Update session cursor
        6. Clear currently_processing
        7. Return result
        """
        processing_item = {
            "type": "conversation",
            "session_id": session_id,
            "message_range": list(message_range),
            "channel": channel,
        }
        self._cursor.set_currently_processing(processing_item)

        try:
            # Build query from message content for related facts search
            query_text = " ".join(
                str(m.get("content", ""))[:100]
                for m in messages[-3:]  # last 3 messages for context
                if isinstance(m.get("content"), str)
            )
            existing_facts = await self._get_related_facts(query_text)

            # Call the memory agent
            result = await self._agent.extract_from_conversation(
                agent_id="",  # agent_id not tracked at this level
                session_id=session_id,
                messages=messages,
                existing_facts=existing_facts,
                registry=self._registry,
            )

            if result.skip_reason:
                logger.debug(
                    "Conversation ingestion skipped for session %s: %s",
                    session_id,
                    result.skip_reason,
                )
                self._cursor.update_session_cursor(session_id, message_range[1], channel)
                return result

            # Process each extraction
            for extraction in result.extractions:
                await self._process_extraction(extraction, session_id, message_range)

            # Update cursor
            self._cursor.update_session_cursor(session_id, message_range[1], channel)

        finally:
            self._cursor.clear_currently_processing()

        return result

    async def ingest_document(
        self,
        file_path: str,
        content: str,
    ) -> ExtractionResult:
        """Ingest a document and extract/update vault facts.

        Steps:
        1. Hash the document content
        2. Check if already processed (same hash in cursor store)
        3. Get existing facts from this source_file
        4. Call memory agent
        5. Reconcile: match new extractions to old facts
        6. Update document cursor
        7. Return result
        """
        content_hash = _hash_content(content)

        # Check if already processed
        existing_cursor = self._cursor.get_document_cursor(file_path)
        if existing_cursor is not None and existing_cursor.last_hash == content_hash:
            logger.debug("Document already processed (same hash): %s", file_path)
            return ExtractionResult(
                extractions=[],
                skip_reason="already_processed_same_hash",
            )

        processing_item = {
            "type": "document",
            "file_path": file_path,
            "hash": content_hash,
        }
        self._cursor.set_currently_processing(processing_item)

        try:
            # Get existing facts from this source_file
            existing_facts = self._store.list_facts(source_file=file_path)

            # Call memory agent
            result = await self._agent.extract_from_document(
                agent_id="",
                document_path=file_path,
                document_content=content,
                existing_facts=existing_facts,
                registry=self._registry,
            )

            if result.skip_reason:
                logger.debug("Document ingestion skipped for %s: %s", file_path, result.skip_reason)
                self._cursor.update_document_cursor(file_path, content_hash, [])
                return result

            # Process extractions
            new_fact_ids: list[str] = []
            for extraction in result.extractions:
                fact_id = await self._process_document_extraction(extraction, file_path)
                if fact_id:
                    new_fact_ids.append(fact_id)

            # Update cursor with new fact IDs
            old_fact_ids = self._cursor.get_extracted_fact_ids(file_path)
            all_fact_ids = list(set(old_fact_ids) | set(new_fact_ids))
            self._cursor.update_document_cursor(file_path, content_hash, all_fact_ids)

        finally:
            self._cursor.clear_currently_processing()

        return result

    async def handle_document_deleted(self, file_path: str) -> list[str]:
        """Handle deletion of a document by archiving its extracted facts.

        Steps:
        1. Get extracted_fact_ids from cursor store
        2. Archive each fact with reason="source_file_deleted"
        3. Remove from cursor store (by updating with empty fact list)
        4. Return archived fact IDs

        Returns:
            List of archived fact IDs.
        """
        fact_ids = self._cursor.get_extracted_fact_ids(file_path)
        archived = []

        for fact_id in fact_ids:
            try:
                self._store.archive_fact(fact_id, reason="source_file_deleted")
                archived.append(fact_id)
            except FileNotFoundError:
                logger.debug("Fact %s already gone when archiving for deleted doc", fact_id)

        # Clear cursor entry
        self._cursor.update_document_cursor(file_path, "", [])

        return archived

    async def run_catch_up(
        self,
        sessions_dir: Path,
        memory_dir: Path,
        skip_channels: Optional[set[str]] = None,
    ) -> dict:
        """Scan for unprocessed sessions and documents and process them.

        Sessions are processed oldest-first.

        Returns:
            Stats dict with keys: sessions_processed, documents_processed,
            facts_created, errors.
        """
        if skip_channels is None:
            skip_channels = _DEFAULT_SKIP_CHANNELS

        stats = {
            "sessions_processed": 0,
            "documents_processed": 0,
            "facts_created": 0,
            "errors": 0,
        }

        # Check crash recovery
        stuck = self._cursor.get_currently_processing()
        if stuck:
            logger.warning(
                "Found stuck processing item from previous run: %s. Clearing.", stuck
            )
            self._cursor.clear_currently_processing()

        # Process unprocessed sessions
        unprocessed = self._cursor.get_unprocessed_sessions(sessions_dir)
        for session_id, _to_process in unprocessed:
            try:
                # Load history for this session
                history_file = sessions_dir / session_id / "history.json"
                if not history_file.exists():
                    continue

                import json
                history_data = json.loads(history_file.read_text(encoding="utf-8"))
                if not isinstance(history_data, list):
                    continue

                # Get current cursor position
                cursor = self._cursor.get_session_cursor(session_id)
                start_idx = cursor.last_message_index if cursor else 0

                messages = history_data[start_idx:]
                if not messages:
                    continue

                message_range = (start_idx, start_idx + len(messages))

                # Detect channel
                session_json = sessions_dir / session_id / "session.json"
                channel = ""
                if session_json.exists():
                    try:
                        sess_data = json.loads(session_json.read_text(encoding="utf-8"))
                        channel = str(sess_data.get("channel", ""))
                    except Exception:
                        pass

                if channel in skip_channels:
                    continue

                result = await self.ingest_conversation_turn(
                    session_id=session_id,
                    messages=messages,
                    message_range=message_range,
                    channel=channel,
                )
                stats["sessions_processed"] += 1
                stats["facts_created"] += len([
                    e for e in result.extractions
                    if e.action == ExtractionAction.CREATE
                ])

            except Exception as exc:
                logger.warning("Error processing session %s: %s", session_id, exc)
                stats["errors"] += 1

        # Process deleted documents
        deleted_docs = self._cursor.get_deleted_documents(memory_dir)
        for file_path in deleted_docs:
            try:
                await self.handle_document_deleted(file_path)
                stats["documents_processed"] += 1
            except Exception as exc:
                logger.warning("Error handling deleted doc %s: %s", file_path, exc)
                stats["errors"] += 1

        # Process new/changed documents
        if memory_dir.exists():
            for md_file in memory_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    result = await self.ingest_document(
                        file_path=str(md_file),
                        content=content,
                    )
                    if result.skip_reason != "already_processed_same_hash":
                        stats["documents_processed"] += 1
                        stats["facts_created"] += len([
                            e for e in result.extractions
                            if e.action == ExtractionAction.CREATE
                        ])
                except Exception as exc:
                    logger.warning("Error processing document %s: %s", md_file, exc)
                    stats["errors"] += 1

        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_related_facts(self, query: str) -> list[VaultFact]:
        """Search for existing facts related to the query."""
        if not query.strip():
            return []
        results = await self._search.search(query, limit=10)
        facts = []
        for result in results:
            fact = result.fact
            if fact is None:
                fact = self._store.read_fact(result.fact_id)
            if fact is not None:
                facts.append(fact)
        return facts

    async def _find_near_duplicate(self, claim: str) -> Optional[VaultFact]:
        """Search for an existing active fact whose claim closely matches claim.

        Uses a two-stage check:
        1. Search by claim text to get candidates.
        2. Compute word-level Jaccard similarity — if >=0.7, treat as duplicate.

        Returns the best-matching existing fact, or None.
        """
        import re as _re

        if not claim.strip():
            return None

        candidates = await self._search.search(claim, limit=5)
        claim_words = set(_re.sub(r"[^\w\s]", "", claim.lower()).split())
        if not claim_words:
            return None

        best_fact: Optional[VaultFact] = None
        best_score = 0.0

        for result in candidates:
            fact = result.fact
            if fact is None:
                fact = self._store.read_fact(result.fact_id)
            if fact is None:
                continue
            if fact.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
                continue
            existing_words = set(_re.sub(r"[^\w\s]", "", fact.claim.lower()).split())
            if not existing_words:
                continue
            intersection = claim_words & existing_words
            union = claim_words | existing_words
            jaccard = len(intersection) / len(union)
            if jaccard >= 0.7 and jaccard > best_score:
                best_score = jaccard
                best_fact = fact

        return best_fact

    async def _auto_link_fact(self, fact: VaultFact, max_links: int = 5) -> VaultFact:
        """Search for related existing facts and populate related_to automatically.

        Runs at write time for every new CREATE so no separate relink pass is
        needed.  Skips near-duplicates (Jaccard >= 0.6) — those are the same
        concept rephrased, not a meaningful cross-topic relationship.

        Returns the (possibly updated) fact.
        """
        import re as _re

        query = fact.claim
        if fact.body:
            query = f"{query} {fact.body[:100]}"

        candidates = await self._search.search(query, limit=max_links * 3)

        claim_words = set(_re.sub(r"[^\w\s]", "", fact.claim.lower()).split())
        seen: set[str] = set(fact.related_to) | {fact.id}
        new_links: list[str] = []

        for result in candidates:
            if len(new_links) >= max_links:
                break
            if result.fact_id in seen:
                continue
            candidate = result.fact
            if candidate is None:
                candidate = self._store.read_fact(result.fact_id)
            if candidate is None:
                continue
            if candidate.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
                continue
            # Skip near-duplicates — same concept, not a useful link
            cand_words = set(_re.sub(r"[^\w\s]", "", candidate.claim.lower()).split())
            if claim_words and cand_words:
                intersection = claim_words & cand_words
                union = claim_words | cand_words
                if len(intersection) / len(union) >= 0.6:
                    continue
            seen.add(result.fact_id)
            new_links.append(result.fact_id)

        if new_links:
            updated_related = list(dict.fromkeys(list(fact.related_to) + new_links))
            fact = fact.model_copy(update={"related_to": updated_related})
            self._store.write_fact(fact)
            logger.debug(
                "Auto-linked fact %s → %d related: %s",
                fact.id, len(new_links), new_links,
            )

        return fact

    def _resolve_and_rewrite_links(self, fact) -> "VaultFact":
        """Parse untyped wikilinks from body, resolve to related_to IDs.

        Kept for backward compatibility. For new facts use
        _resolve_and_rewrite_typed_links which handles all link types.
        """
        from .links import resolve_fact_links
        all_facts = self._store.list_facts()
        resolved = resolve_fact_links(fact, all_facts)
        if resolved:
            new_related = list(dict.fromkeys(list(fact.related_to) + resolved))
            fact = fact.model_copy(update={"related_to": new_related})
            self._store.write_fact(fact)
            logger.debug(
                "Resolved %d wikilink(s) for fact %s: %s",
                len(resolved), fact.id, resolved,
            )
        return fact

    def _resolve_and_rewrite_typed_links(self, fact: VaultFact) -> VaultFact:
        """Parse typed wikilinks from body, populate depends_on/part_of/contradicts/related_to.

        Supersedes _resolve_and_rewrite_links for new facts — handles all link types.
        Only rewrites to disk if any fields changed.
        """
        from .links import resolve_fact_typed_links
        all_facts = self._store.list_facts()
        by_type = resolve_fact_typed_links(fact, all_facts)

        updates: dict = {}

        new_related = list(dict.fromkeys(list(fact.related_to) + by_type.get("related_to", [])))
        if new_related != list(fact.related_to):
            updates["related_to"] = new_related

        new_depends = list(dict.fromkeys(list(fact.depends_on) + by_type.get("depends_on", [])))
        if new_depends != list(fact.depends_on):
            updates["depends_on"] = new_depends

        new_contradicts = list(dict.fromkeys(list(fact.contradicts) + by_type.get("contradicts", [])))
        if new_contradicts != list(fact.contradicts):
            updates["contradicts"] = new_contradicts

        part_of_ids = by_type.get("part_of", [])
        if part_of_ids and fact.part_of is None:
            updates["part_of"] = part_of_ids[0]  # single parent

        if updates:
            fact = fact.model_copy(update=updates)
            self._store.write_fact(fact)
            logger.debug(
                "Typed wikilinks resolved for fact %s: %s",
                fact.id, {k: v for k, v in updates.items()},
            )

        return fact

    async def _reweave_fact(self, fact: VaultFact) -> VaultFact:
        """Backward supersession pass: auto-supersede stale contradictory facts.

        Two passes:
        1. Explicit: supersede all facts listed in fact.contradicts (LLM-declared)
        2. Jaccard:  supersede same-type facts with Jaccard overlap in [0.40, 0.64]
                     (same topic, same type, newer = updated version of old)

        Returns the (possibly updated) fact after any supersession chain.
        """
        import re as _re
        from datetime import timezone

        now = datetime.now(timezone.utc)
        already_superseded: set[str] = set()

        # --- Pass 1: explicit contradicts links ---
        for target_id in list(fact.contradicts):
            if target_id in already_superseded:
                continue
            try:
                old_fact, updated_new = self._store.supersede_fact(target_id, fact)
                await self._search.remove_fact(old_fact.id)
                await self._search.index_fact(updated_new)
                fact = updated_new
                already_superseded.add(target_id)
                logger.info(
                    "Reweave (explicit contradicts): fact %s supersedes %s — %r → %r",
                    fact.id, target_id,
                    old_fact.claim[:50], fact.claim[:50],
                )
            except FileNotFoundError:
                logger.debug("Reweave: contradicts target not found: %s", target_id)

        # --- Pass 2: Jaccard-based same-type supersession ---
        _TOPIC_STOPWORDS = {
            "user", "prefers", "prefer", "uses", "use", "always", "never",
            "the", "a", "an", "is", "are", "was", "has", "have", "be",
            "to", "of", "in", "on", "for", "with", "and", "or", "not",
        }

        claim_words = set(_re.sub(r"[^\w\s]", "", fact.claim.lower()).split())
        substantive = claim_words - _TOPIC_STOPWORDS

        # Skip if claim is too short to get meaningful signal
        if len(claim_words) < 5 or not substantive:
            return fact

        candidates = await self._search.search(fact.claim, limit=10)
        first_superseded = False

        for result in candidates:
            if result.fact_id == fact.id or result.fact_id in already_superseded:
                continue
            candidate = result.fact
            if candidate is None:
                candidate = self._store.read_fact(result.fact_id)
            if candidate is None:
                continue
            if candidate.state in (VaultFactState.SUPERSEDED, VaultFactState.ARCHIVED):
                continue
            if candidate.type != fact.type:
                continue

            cand_words = set(_re.sub(r"[^\w\s]", "", candidate.claim.lower()).split())
            if not cand_words:
                continue

            intersection = claim_words & cand_words
            union_words = claim_words | cand_words
            jaccard = len(intersection) / len(union_words)

            # Overlap must be in the "same topic, updated" range
            if not (0.40 <= jaccard <= 0.64):
                continue

            # At least one substantive non-stopword must overlap
            if not (intersection - _TOPIC_STOPWORDS):
                continue

            try:
                if not first_superseded:
                    old_fact, updated_new = self._store.supersede_fact(result.fact_id, fact)
                    await self._search.remove_fact(old_fact.id)
                    await self._search.index_fact(updated_new)
                    fact = updated_new
                    first_superseded = True
                else:
                    # Additional matches: mark superseded directly
                    additional = self._store.read_fact(result.fact_id)
                    if additional:
                        updated = additional.model_copy(update={
                            "state": VaultFactState.SUPERSEDED,
                            "superseded_by": fact.id,
                            "valid_until": now,
                        })
                        self._store.write_fact(updated)
                        await self._search.remove_fact(updated.id)
                already_superseded.add(result.fact_id)
                logger.info(
                    "Reweave (Jaccard %.2f, type=%s): fact %s supersedes %s — %r → %r",
                    jaccard, fact.type,
                    fact.id, result.fact_id,
                    candidate.claim[:50], fact.claim[:50],
                )
            except FileNotFoundError:
                logger.debug("Reweave: supersede target disappeared: %s", result.fact_id)

        return fact

    async def _process_extraction(
        self,
        extraction: FactExtraction,
        session_id: str,
        message_range: tuple[int, int],
    ) -> Optional[str]:
        """Process a single extraction action. Returns fact_id or None."""
        source_session = SourceSession(session_id=session_id, message_range=message_range)

        if extraction.action == ExtractionAction.CREATE:
            # Dedup gate: if a near-duplicate already exists, reinforce it
            # instead of creating another copy.
            duplicate = await self._find_near_duplicate(extraction.fact_fields.get("claim", ""))
            if duplicate:
                logger.debug(
                    "Dedup: reinforcing %s instead of creating duplicate for %r",
                    duplicate.id,
                    str(extraction.fact_fields.get("claim", ""))[:60],
                )
                updated = self._store.reinforce_fact(duplicate.id, source_session)
                await self._search.index_fact(updated)
                return duplicate.id
            fact = _build_fact_from_extraction(extraction, session_id, message_range)
            self._store.write_fact(fact)
            fact = self._resolve_and_rewrite_typed_links(fact)
            await self._search.index_fact(fact)
            fact = await self._auto_link_fact(fact)
            fact = await self._reweave_fact(fact)
            logger.debug("Created fact %s: %s", fact.id, fact.claim[:60])
            return fact.id

        elif extraction.action == ExtractionAction.REINFORCE:
            if not extraction.target_id:
                logger.warning("REINFORCE extraction missing target_id, skipping")
                return None
            try:
                updated = self._store.reinforce_fact(extraction.target_id, source_session)
                await self._search.index_fact(updated)
                logger.debug("Reinforced fact %s", extraction.target_id)
                return extraction.target_id
            except FileNotFoundError:
                logger.warning("REINFORCE target not found: %s", extraction.target_id)
                return None

        elif extraction.action == ExtractionAction.SUPERSEDE:
            if not extraction.supersedes_id:
                logger.warning("SUPERSEDE extraction missing supersedes_id, skipping")
                return None
            new_fact = _build_fact_from_extraction(extraction, session_id, message_range)
            try:
                old_fact, updated_new = self._store.supersede_fact(extraction.supersedes_id, new_fact)
                await self._search.remove_fact(old_fact.id)
                await self._search.index_fact(updated_new)
                logger.debug(
                    "Superseded fact %s → %s", extraction.supersedes_id, updated_new.id
                )
                return updated_new.id
            except FileNotFoundError:
                logger.warning("SUPERSEDE target not found: %s", extraction.supersedes_id)
                return None

        return None

    async def _process_document_extraction(
        self,
        extraction: FactExtraction,
        file_path: str,
    ) -> Optional[str]:
        """Process a single document extraction. Returns fact_id or None."""
        if extraction.action == ExtractionAction.CREATE:
            # Dedup gate: reinforce near-duplicate instead of creating a copy
            duplicate = await self._find_near_duplicate(extraction.fact_fields.get("claim", ""))
            if duplicate:
                logger.debug(
                    "Dedup: reinforcing %s instead of creating duplicate for %r",
                    duplicate.id,
                    str(extraction.fact_fields.get("claim", ""))[:60],
                )
                source = SourceSession(session_id="document", message_range=(0, 0))
                updated = self._store.reinforce_fact(duplicate.id, source)
                await self._search.index_fact(updated)
                return duplicate.id
            fact = _build_fact_from_document_extraction(extraction, file_path)
            self._store.write_fact(fact)
            fact = self._resolve_and_rewrite_typed_links(fact)
            await self._search.index_fact(fact)
            fact = await self._auto_link_fact(fact)
            fact = await self._reweave_fact(fact)
            logger.debug("Created document fact %s from %s", fact.id, file_path)
            return fact.id

        elif extraction.action == ExtractionAction.REINFORCE:
            if not extraction.target_id:
                return None
            try:
                # Use a dummy session for document reinforcement
                source = SourceSession(session_id="document", message_range=(0, 0))
                updated = self._store.reinforce_fact(extraction.target_id, source)
                await self._search.index_fact(updated)
                return extraction.target_id
            except FileNotFoundError:
                logger.warning("REINFORCE target not found: %s", extraction.target_id)
                return None

        elif extraction.action == ExtractionAction.SUPERSEDE:
            if not extraction.supersedes_id:
                return None
            new_fact = _build_fact_from_document_extraction(extraction, file_path)
            try:
                old_fact, updated_new = self._store.supersede_fact(extraction.supersedes_id, new_fact)
                await self._search.remove_fact(old_fact.id)
                await self._search.index_fact(updated_new)
                return updated_new.id
            except FileNotFoundError:
                logger.warning("SUPERSEDE target not found: %s", extraction.supersedes_id)
                return None

        return None
