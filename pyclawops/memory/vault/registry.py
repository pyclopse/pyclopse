"""TypeSchemaRegistry — manages built-in and custom memory types.

Built-in types are defined with descriptions and keyword lists for
automatic classification of text into memory types.
"""

from __future__ import annotations

from typing import Optional

from .models import TypeSchema

# ---------------------------------------------------------------------------
# Built-in type definitions
# ---------------------------------------------------------------------------

BUILTIN_TYPES: list[TypeSchema] = [
    TypeSchema(
        name="preference",
        description="User likes, dislikes, habits, style choices",
        keywords=[
            "prefer", "always use", "never use", "like", "dislike",
            "hate", "love", "rather", "instead of", "favorite",
        ],
    ),
    TypeSchema(
        name="fact",
        description="Stable factual info about user/context",
        keywords=["works at", "lives in", "uses", "has been", "is located"],
    ),
    TypeSchema(
        name="decision",
        description="A decision that was made",
        keywords=[
            "decided", "going with", "chose", "we will use",
            "picked", "selected", "we chose", "final decision",
        ],
    ),
    TypeSchema(
        name="lesson",
        description="Something learned from experience",
        keywords=[
            "learned", "found that", "realized", "turns out",
            "mistake", "next time", "should have", "in hindsight",
        ],
    ),
    TypeSchema(
        name="commitment",
        description="A promise or agreement made",
        keywords=[
            "will", "promised", "committed", "by friday", "by monday",
            "need to", "must", "agreed to", "going to",
        ],
    ),
    TypeSchema(
        name="person",
        description="Information about a person",
        keywords=[
            "is the", "works on", "manages", "reports to",
            "contact", "their name is", "he is", "she is",
        ],
    ),
    TypeSchema(
        name="hypothesis",
        description="Tentative belief needing confirmation",
        keywords=[
            "might", "maybe", "possibly", "seems like",
            "i think", "could be", "not sure but", "possibly",
        ],
    ),
    TypeSchema(
        name="absence",
        description="Explicitly noted non-existence",
        keywords=[
            "don't have", "no ", "never had", "missing",
            "lacks", "without", "doesn't exist", "not available",
        ],
    ),
    TypeSchema(
        name="anti",
        description="Temporary suppression - do not memorize this",
        keywords=[],
    ),
]


class TypeSchemaRegistry:
    """Manages built-in and custom memory type schemas.

    Supports keyword-based classification of text into memory types.
    """

    def __init__(self, custom_types: Optional[list[TypeSchema]] = None) -> None:
        self._types: dict[str, TypeSchema] = {}
        for schema in BUILTIN_TYPES:
            self._types[schema.name] = schema
        if custom_types:
            for schema in custom_types:
                self._types[schema.name] = schema

    def register(self, schema: TypeSchema) -> None:
        """Register or replace a type schema."""
        self._types[schema.name] = schema

    def get(self, name: str) -> Optional[TypeSchema]:
        """Get a type schema by name. Returns None if not found."""
        return self._types.get(name)

    def all_types(self) -> list[TypeSchema]:
        """Return all registered type schemas (built-in + custom)."""
        return list(self._types.values())

    def is_valid_type(self, name: str) -> bool:
        """Return True if name is a registered type."""
        return name in self._types

    def classify(self, text: str) -> tuple[str, float]:
        """Keyword-based classification. Returns (type_name, confidence).

        Scores each type by keyword hits in the text.
        Returns the highest-scoring type.
        Falls back to 'fact' with low confidence if no hits.

        Args:
            text: Text to classify.

        Returns:
            Tuple of (type_name, confidence) where confidence is 0.0-1.0.
        """
        text_lower = text.lower()
        best_type = "fact"
        best_score = 0

        for schema in self._types.values():
            if not schema.keywords:
                continue
            score = sum(1 for kw in schema.keywords if kw.lower() in text_lower)
            if score > best_score:
                best_score = score
                best_type = schema.name

        if best_score == 0:
            return "fact", 0.3

        # Normalize confidence: more keyword hits = higher confidence, cap at 0.95
        max_keywords = max(len(s.keywords) for s in self._types.values() if s.keywords)
        confidence = min(0.95, 0.5 + (best_score / max(max_keywords, 1)) * 0.45)
        return best_type, round(confidence, 3)

    def memory_agent_type_list(self) -> str:
        """Format all types as a string for the memory agent system prompt."""
        lines = []
        for schema in self._types.values():
            kw_hint = ""
            if schema.keywords:
                sample = schema.keywords[:3]
                kw_hint = f" (e.g. {', '.join(repr(k) for k in sample)})"
            lines.append(f"- **{schema.name}**: {schema.description}{kw_hint}")
        return "\n".join(lines)
