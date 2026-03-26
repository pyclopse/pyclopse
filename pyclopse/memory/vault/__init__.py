"""pyclopse vault memory system.

A standalone, self-contained structured memory vault for pyclopse agents.
Facts are stored as markdown files with YAML frontmatter and organized
by lifecycle state (provisional → crystallized → archived).
"""

from .agent import FastAgentMemoryAgent, MemoryAgent, MockMemoryAgent
from .config import VaultConfig
from .cursor import CursorStore
from .ingestion import IngestionHandler
from .lifecycle import LifecycleManager
from .models import (
    ExtractionResult,
    MemoryType,
    RetrievalProfile,
    VaultContext,
    VaultFact,
    VaultFactState,
)
from .registry import TypeSchemaRegistry
from .retrieval import ContextAssembler, infer_profile
from .search import FallbackSearchBackend, SearchResult
from .store import VaultStore
from .ulid import generate as generate_ulid

__all__ = [
    # Models
    "VaultFact",
    "MemoryType",
    "VaultFactState",
    "RetrievalProfile",
    "VaultContext",
    "ExtractionResult",
    # Core components
    "VaultStore",
    "CursorStore",
    "TypeSchemaRegistry",
    "LifecycleManager",
    "ContextAssembler",
    "infer_profile",
    "FallbackSearchBackend",
    "SearchResult",
    "MemoryAgent",
    "FastAgentMemoryAgent",
    "MockMemoryAgent",
    "IngestionHandler",
    "VaultConfig",
    "generate_ulid",
]
