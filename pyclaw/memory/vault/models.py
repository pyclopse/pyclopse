"""Pydantic models for the vault memory system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import ulid as _ulid


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    """Built-in memory type names."""

    USER = "user"          # Personal identity/background facts about the user
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    LESSON = "lesson"
    COMMITMENT = "commitment"
    GOAL = "goal"          # Objective the user is working toward (longer-horizon than commitment)
    PERSON = "person"
    HYPOTHESIS = "hypothesis"
    ABSENCE = "absence"
    ANTI = "anti"
    CONTEXT = "context"    # Environment/setup facts: OS, hardware, dev tools, language versions
    PROJECT = "project"    # Ongoing project — acts as a named anchor for related facts
    RULE = "rule"          # Behavioral constraint/mandate — never auto-injected; queryable explicitly


class VaultFactState(str, Enum):
    """Lifecycle state of a vault fact."""

    PROVISIONAL = "provisional"
    CRYSTALLIZED = "crystallized"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class RetrievalProfile(str, Enum):
    """Retrieval context profiles that shape result ordering."""

    DEFAULT = "default"
    PLANNING = "planning"
    INCIDENT = "incident"
    HANDOFF = "handoff"
    RESEARCH = "research"


class ExtractionAction(str, Enum):
    """What the memory agent decided to do with an extracted fact."""

    CREATE = "create"
    REINFORCE = "reinforce"
    SUPERSEDE = "supersede"


# ---------------------------------------------------------------------------
# Core fact model
# ---------------------------------------------------------------------------


class SourceSession(BaseModel):
    """Reference to a conversation session that contributed to a fact."""

    session_id: str
    message_range: tuple[int, int]  # [start_index, end_index]


class VaultFact(BaseModel):
    """A single atomic memory fact stored in the vault."""

    id: str = Field(default_factory=_ulid.generate)
    type: str = MemoryType.FACT  # MemoryType value or custom string
    state: VaultFactState = VaultFactState.PROVISIONAL
    claim: str  # One atomic fact statement
    contrastive: Optional[str] = None  # "X over Y because Z"
    implied: bool = False  # True = inferred, not explicitly stated
    confidence: float = 0.7  # 0.0-1.0
    reinforcement_count: int = 0
    surprise_score: float = 0.0  # 0.0-1.0, high = agent was corrected
    event_at: Optional[datetime] = None
    stated_at: Optional[datetime] = None
    written_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None  # for anti-memories
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None  # set when superseded
    source_sessions: list[SourceSession] = Field(default_factory=list)
    source_file: Optional[str] = None  # if extracted from a memory document
    supersedes: Optional[str] = None  # ULID of fact this replaces
    superseded_by: Optional[str] = None  # ULID of fact that replaced this
    related_to: list[str] = Field(default_factory=list)  # ULIDs of related facts (generic similarity)
    depends_on: list[str] = Field(default_factory=list)  # ULIDs: this fact requires these to be true
    part_of: Optional[str] = None                        # ULID: parent/container fact or project
    contradicts: list[str] = Field(default_factory=list) # ULIDs: explicit contradictions (triggers reweave)
    tier: int = 1  # 1=full, 2=fact-only, 3=summary, 4=tags
    body: str = ""  # markdown body (narrative context, may contain [[wikilinks]])


# ---------------------------------------------------------------------------
# Type schema
# ---------------------------------------------------------------------------


class TypeSchemaFieldDef(BaseModel):
    """Definition of a custom field within a memory type schema."""

    type: str = "string"  # string | number | boolean | date | list
    required: bool = False
    default: Optional[Any] = None
    enum: Optional[list[str]] = None
    description: str = ""


class TypeSchema(BaseModel):
    """Schema for a memory type (built-in or custom)."""

    name: str
    description: str
    keywords: list[str] = Field(default_factory=list)
    fields: dict[str, TypeSchemaFieldDef] = Field(default_factory=dict)
    color: Optional[str] = None  # hex color for UI


# ---------------------------------------------------------------------------
# Cursor models
# ---------------------------------------------------------------------------


class SessionCursor(BaseModel):
    """Tracks how far vault processing has progressed in a session."""

    session_id: str
    last_message_index: int = 0
    last_processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    channel: str = ""


class DocumentCursor(BaseModel):
    """Tracks vault processing state for a memory document file."""

    file_path: str
    last_hash: str = ""
    last_processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extracted_fact_ids: list[str] = Field(default_factory=list)


class CursorStoreData(BaseModel):
    """Top-level data model for the cursor store JSON file."""

    currently_processing: Optional[dict[str, Any]] = None
    sessions: dict[str, SessionCursor] = Field(default_factory=dict)
    documents: dict[str, DocumentCursor] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Retrieval / context models
# ---------------------------------------------------------------------------


class VaultContext(BaseModel):
    """Assembled retrieval context ready for injection into a prompt."""

    facts: list[VaultFact] = Field(default_factory=list)
    document_refs: list[str] = Field(default_factory=list)
    profile: RetrievalProfile = RetrievalProfile.DEFAULT
    query: str = ""


# ---------------------------------------------------------------------------
# Extraction models
# ---------------------------------------------------------------------------


class FactExtraction(BaseModel):
    """A single extraction decision returned by the memory agent."""

    action: ExtractionAction
    # Fields for the fact (used for CREATE and SUPERSEDE new fact)
    fact_fields: dict[str, Any] = Field(default_factory=dict)
    # For REINFORCE: ULID of existing fact to reinforce
    target_id: Optional[str] = None
    # For SUPERSEDE: ULID of existing fact to supersede
    supersedes_id: Optional[str] = None


class ExtractionResult(BaseModel):
    """Result returned by the memory agent after processing a conversation/document."""

    extractions: list[FactExtraction] = Field(default_factory=list)
    skip_reason: Optional[str] = None  # Non-None means nothing was extracted


# ---------------------------------------------------------------------------
# Lifecycle stats
# ---------------------------------------------------------------------------


class LifecycleStats(BaseModel):
    """Statistics from a lifecycle maintenance run."""

    crystallized: int = 0
    forgotten: int = 0
    compressed: int = 0
    reaped: int = 0
    hypotheses_promoted: int = 0
    hypotheses_archived: int = 0

    def merge(self, other: "LifecycleStats") -> "LifecycleStats":
        """Return a new LifecycleStats that is the sum of self and other."""
        return LifecycleStats(
            crystallized=self.crystallized + other.crystallized,
            forgotten=self.forgotten + other.forgotten,
            compressed=self.compressed + other.compressed,
            reaped=self.reaped + other.reaped,
            hypotheses_promoted=self.hypotheses_promoted + other.hypotheses_promoted,
            hypotheses_archived=self.hypotheses_archived + other.hypotheses_archived,
        )
