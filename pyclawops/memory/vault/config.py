"""Pydantic config models for the vault memory system."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TypeSchemaFieldDef(BaseModel):
    """Definition of a custom field within a memory type schema."""

    type: str = "string"  # string | number | boolean | date | list
    required: bool = False
    default: Optional[Any] = None
    enum: Optional[list[str]] = None
    description: str = ""


class TypeSchemaConfig(BaseModel):
    """User-defined memory type schema (from pyclawops.yaml)."""

    name: str
    description: str
    keywords: list[str] = Field(default_factory=list)
    fields: dict[str, TypeSchemaFieldDef] = Field(default_factory=dict)
    color: Optional[str] = None  # hex color for UI


class VaultLifecycleConfig(BaseModel):
    """Configuration for vault fact lifecycle management."""

    crystallize_reinforcements: int = 3
    crystallize_days: int = 7
    forget_days: int = 30
    tier1_to_2_days: int = 30
    tier2_to_3_days: int = 90
    tier3_to_4_days: int = 365


class VaultSearchConfig(BaseModel):
    """Configuration for vault search backend."""

    backend: str = "fallback"  # fallback | qmd | hybrid
    qmd_path: str = ""         # path to qmd binary; defaults to "qmd" on PATH
    qmd_collection: str = ""   # qmd collection name; defaults to "{agent_id}-vault"
    injection_limit: int = 5           # max facts injected per query
    confidence_threshold: float = 0.5
    min_relevance_score: float = 0.5  # 0–1 normalized; facts below this score are not injected
    min_query_words: int = 3          # skip injection if query has fewer than this many words
    graph_hops: int = 2        # wikilink BFS depth for context expansion


class VaultAgentConfig(BaseModel):
    """Configuration for the vault memory extraction agent."""

    enabled: bool = True
    model: str = ""  # empty = use main agent model
    max_tokens: int = 2048
    channels: list[str] = Field(
        default_factory=lambda: ["telegram", "slack", "tui", "http"]
    )
    min_turns: int = 2


class VaultConfig(BaseModel):
    """Top-level vault configuration."""

    enabled: bool = True
    path: str = ""  # empty = default path under agent dir
    show_recall: bool = False  # prepend injected facts to agent replies (not saved to history)
    agent: VaultAgentConfig = Field(default_factory=VaultAgentConfig)
    lifecycle: VaultLifecycleConfig = Field(default_factory=VaultLifecycleConfig)
    search: VaultSearchConfig = Field(default_factory=VaultSearchConfig)
    types: list[TypeSchemaConfig] = Field(default_factory=list)
    default_profile: str = "auto"  # auto | default | planning | incident | handoff | research
