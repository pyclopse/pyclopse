"""Tests for TypeSchemaRegistry."""

from __future__ import annotations

import pytest

from pyclaw.memory.vault.models import TypeSchema
from pyclaw.memory.vault.registry import BUILTIN_TYPES, TypeSchemaRegistry


class TestBuiltinTypes:
    def test_builtin_types_present(self):
        reg = TypeSchemaRegistry()
        names = {t.name for t in reg.all_types()}
        expected = {"preference", "fact", "decision", "lesson", "commitment", "person", "hypothesis", "absence", "anti"}
        assert expected.issubset(names)

    def test_get_builtin_type(self):
        reg = TypeSchemaRegistry()
        schema = reg.get("preference")
        assert schema is not None
        assert schema.name == "preference"
        assert len(schema.keywords) > 0

    def test_get_unknown_type_returns_none(self):
        reg = TypeSchemaRegistry()
        assert reg.get("nonexistent_type_xyz") is None

    def test_all_builtin_have_descriptions(self):
        reg = TypeSchemaRegistry()
        for t in reg.all_types():
            assert t.description, f"Type {t.name} has no description"


class TestClassification:
    def test_classify_preference(self):
        reg = TypeSchemaRegistry()
        type_name, confidence = reg.classify("I prefer Python over Go for scripting")
        assert type_name == "preference"
        assert confidence > 0.3

    def test_classify_decision(self):
        reg = TypeSchemaRegistry()
        type_name, confidence = reg.classify("We decided to use PostgreSQL for the database")
        assert type_name == "decision"
        assert confidence > 0.3

    def test_classify_lesson(self):
        reg = TypeSchemaRegistry()
        type_name, confidence = reg.classify("I learned that early optimization is a mistake")
        assert type_name == "lesson"
        assert confidence > 0.3

    def test_classify_commitment(self):
        reg = TypeSchemaRegistry()
        type_name, confidence = reg.classify("I promised to deliver by Friday")
        # "promised" and "by friday" are commitment keywords
        assert type_name == "commitment"
        assert confidence > 0.3

    def test_classify_fallback(self):
        """Completely unrecognized text should fall back to 'fact' with low confidence."""
        reg = TypeSchemaRegistry()
        type_name, confidence = reg.classify("xyzzy plugh twisty little passages")
        assert type_name == "fact"
        assert confidence <= 0.3

    def test_classify_returns_tuple(self):
        reg = TypeSchemaRegistry()
        result = reg.classify("some text")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], float)

    def test_classify_confidence_between_0_and_1(self):
        reg = TypeSchemaRegistry()
        texts = [
            "I prefer Python",
            "We decided to use Postgres",
            "xyzzy random text",
            "I learned from this mistake",
        ]
        for text in texts:
            _, conf = reg.classify(text)
            assert 0.0 <= conf <= 1.0, f"Confidence {conf} out of range for: {text}"


class TestCustomTypeRegistration:
    def test_custom_type_registration(self):
        reg = TypeSchemaRegistry()
        custom = TypeSchema(
            name="project",
            description="Project-related information",
            keywords=["project", "milestone", "sprint", "deadline"],
        )
        reg.register(custom)
        assert reg.is_valid_type("project")
        assert reg.get("project") is not None

    def test_custom_type_appears_in_all_types(self):
        reg = TypeSchemaRegistry()
        custom = TypeSchema(name="mytype", description="My custom type", keywords=["myword"])
        reg.register(custom)
        names = {t.name for t in reg.all_types()}
        assert "mytype" in names

    def test_custom_type_classification(self):
        reg = TypeSchemaRegistry()
        custom = TypeSchema(
            name="emoji_preference",
            description="Emoji usage preference",
            keywords=["emoji", "emojis", "use emoji"],
        )
        reg.register(custom)
        type_name, confidence = reg.classify("Please always use emoji in responses")
        assert type_name == "emoji_preference"

    def test_custom_type_via_constructor(self):
        custom = TypeSchema(name="custom1", description="Test", keywords=["alpha", "beta"])
        reg = TypeSchemaRegistry(custom_types=[custom])
        assert reg.is_valid_type("custom1")

    def test_custom_overrides_builtin(self):
        """Custom type with same name as builtin should replace it."""
        custom = TypeSchema(
            name="preference",
            description="Custom preference override",
            keywords=["custom_keyword"],
        )
        reg = TypeSchemaRegistry(custom_types=[custom])
        schema = reg.get("preference")
        assert schema.description == "Custom preference override"


class TestIsValidType:
    def test_valid_builtin(self):
        reg = TypeSchemaRegistry()
        assert reg.is_valid_type("preference") is True
        assert reg.is_valid_type("fact") is True

    def test_invalid_type_name(self):
        reg = TypeSchemaRegistry()
        assert reg.is_valid_type("does_not_exist") is False
        assert reg.is_valid_type("") is False


class TestMemoryAgentTypeList:
    def test_memory_agent_type_list_format(self):
        reg = TypeSchemaRegistry()
        result = reg.memory_agent_type_list()
        assert isinstance(result, str)
        # Should contain all built-in type names
        assert "preference" in result
        assert "decision" in result
        assert "lesson" in result
        # Should use markdown bold for type name
        assert "**preference**" in result

    def test_type_list_has_descriptions(self):
        reg = TypeSchemaRegistry()
        result = reg.memory_agent_type_list()
        # Each description should appear somewhere
        assert "User likes" in result  # preference description
        assert "decision" in result
