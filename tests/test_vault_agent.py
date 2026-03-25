"""Tests for vault memory agent — parsing, prompt building, and integration."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyclawops.memory.vault.agent import (
    FastAgentMemoryAgent,
    _fmt_existing_facts,
    _fmt_transcript,
    _parse_extraction_response,
)
from pyclawops.memory.vault.models import (
    ExtractionAction,
    ExtractionResult,
    FactExtraction,
    VaultFact,
)
from pyclawops.memory.vault.registry import TypeSchemaRegistry


# ---------------------------------------------------------------------------
# _parse_extraction_response — unit tests
# ---------------------------------------------------------------------------


def _make_json(**kwargs) -> str:
    base = {"extractions": [], "skip_reason": None}
    base.update(kwargs)
    return json.dumps(base)


def test_parse_empty_skip():
    result = _parse_extraction_response(
        json.dumps({"extractions": [], "skip_reason": "debugging session"})
    )
    assert result.skip_reason == "debugging session"
    assert result.extractions == []


def test_parse_create_action():
    payload = {
        "extractions": [
            {
                "action": "create",
                "type": "preference",
                "claim": "User prefers tabs over spaces",
                "contrastive": "tabs over spaces because of alignment",
                "implied": False,
                "confidence": 0.9,
                "surprise_score": 0.0,
                "body": "Mentioned in code review",
                "target_id": None,
                "supersedes_id": None,
            }
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    assert len(result.extractions) == 1
    e = result.extractions[0]
    assert e.action == ExtractionAction.CREATE
    assert e.fact_fields["type"] == "preference"
    assert e.fact_fields["claim"] == "User prefers tabs over spaces"
    assert e.fact_fields["contrastive"] == "tabs over spaces because of alignment"
    assert e.fact_fields["confidence"] == 0.9
    assert e.fact_fields["surprise_score"] == 0.0
    assert e.fact_fields["implied"] is False
    assert e.target_id is None
    assert e.supersedes_id is None
    assert result.skip_reason is None


def test_parse_reinforce_action():
    payload = {
        "extractions": [
            {
                "action": "reinforce",
                "type": "preference",
                "claim": "User prefers tabs over spaces",
                "contrastive": None,
                "implied": False,
                "confidence": 0.95,
                "surprise_score": 0.0,
                "body": "",
                "target_id": "01JTEST123456789ABCDEFGHIJ",
                "supersedes_id": None,
            }
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    e = result.extractions[0]
    assert e.action == ExtractionAction.REINFORCE
    assert e.target_id == "01JTEST123456789ABCDEFGHIJ"
    assert e.supersedes_id is None


def test_parse_supersede_action():
    payload = {
        "extractions": [
            {
                "action": "supersede",
                "type": "decision",
                "claim": "Project uses PostgreSQL",
                "contrastive": "PostgreSQL over SQLite because of scale",
                "implied": False,
                "confidence": 0.92,
                "surprise_score": 0.3,
                "body": "Changed from SQLite",
                "target_id": None,
                "supersedes_id": "01JOLD123456789ABCDEFGHIJ",
            }
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    e = result.extractions[0]
    assert e.action == ExtractionAction.SUPERSEDE
    assert e.supersedes_id == "01JOLD123456789ABCDEFGHIJ"
    assert e.fact_fields["surprise_score"] == 0.3


def test_parse_strips_markdown_fences():
    payload = {"extractions": [], "skip_reason": "nothing useful"}
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    result = _parse_extraction_response(wrapped)
    assert result.skip_reason == "nothing useful"


def test_parse_strips_plain_code_fence():
    payload = {"extractions": [], "skip_reason": "nothing useful"}
    wrapped = f"```\n{json.dumps(payload)}\n```"
    result = _parse_extraction_response(wrapped)
    assert result.skip_reason == "nothing useful"


def test_parse_extracts_json_from_noisy_text():
    payload = {"extractions": [], "skip_reason": "ok"}
    noisy = f"Here is my analysis:\n{json.dumps(payload)}\nThat's it."
    result = _parse_extraction_response(noisy)
    assert result.skip_reason == "ok"


def test_parse_error_returns_skip():
    result = _parse_extraction_response("This is not JSON at all.")
    assert result.skip_reason == "parse_error"
    assert result.extractions == []


def test_parse_unknown_action_defaults_to_create():
    payload = {
        "extractions": [
            {
                "action": "unknown_action",
                "type": "fact",
                "claim": "Something",
                "contrastive": None,
                "implied": False,
                "confidence": 0.7,
                "surprise_score": 0.0,
                "body": "",
                "target_id": None,
                "supersedes_id": None,
            }
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    assert result.extractions[0].action == ExtractionAction.CREATE


def test_parse_multiple_extractions():
    payload = {
        "extractions": [
            {
                "action": "create",
                "type": "preference",
                "claim": "User prefers Python",
                "contrastive": None,
                "implied": False,
                "confidence": 0.85,
                "surprise_score": 0.0,
                "body": "",
                "target_id": None,
                "supersedes_id": None,
            },
            {
                "action": "create",
                "type": "decision",
                "claim": "Using FastAPI for the REST layer",
                "contrastive": "FastAPI over Flask because of async support",
                "implied": False,
                "confidence": 0.9,
                "surprise_score": 0.0,
                "body": "Decided in architecture review",
                "target_id": None,
                "supersedes_id": None,
            },
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    assert len(result.extractions) == 2
    assert result.extractions[1].fact_fields["contrastive"] == "FastAPI over Flask because of async support"


def test_parse_null_target_and_supersedes_become_none():
    """Explicit nulls in JSON should become Python None, not the string "null"."""
    payload = {
        "extractions": [
            {
                "action": "create",
                "type": "fact",
                "claim": "Something",
                "contrastive": None,
                "implied": False,
                "confidence": 0.7,
                "surprise_score": 0.0,
                "body": "",
                "target_id": None,
                "supersedes_id": None,
            }
        ],
        "skip_reason": None,
    }
    result = _parse_extraction_response(json.dumps(payload))
    e = result.extractions[0]
    assert e.target_id is None
    assert e.supersedes_id is None


# ---------------------------------------------------------------------------
# _fmt_existing_facts
# ---------------------------------------------------------------------------


def test_fmt_existing_facts_empty():
    assert _fmt_existing_facts([]) == ""


def test_fmt_existing_facts_with_facts():
    f = VaultFact(claim="User prefers TypeScript", type="preference")
    text = _fmt_existing_facts([f])
    assert f.id in text
    assert "User prefers TypeScript" in text
    assert "preference" in text


def test_fmt_existing_facts_with_contrastive():
    f = VaultFact(
        claim="User prefers PostgreSQL",
        type="decision",
        contrastive="PostgreSQL over SQLite because of scale",
    )
    text = _fmt_existing_facts([f])
    assert "PostgreSQL over SQLite" in text


def test_fmt_existing_facts_caps_at_30():
    facts = [VaultFact(claim=f"Fact {i}", type="fact") for i in range(50)]
    text = _fmt_existing_facts(facts)
    # Should have at most 30 facts plus header line
    fact_lines = [l for l in text.splitlines() if l.startswith("- [")]
    assert len(fact_lines) == 30


# ---------------------------------------------------------------------------
# _fmt_transcript
# ---------------------------------------------------------------------------


def test_fmt_transcript_basic():
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    text = _fmt_transcript(messages)
    assert "User: Hello" in text
    assert "Assistant: Hi there" in text


def test_fmt_transcript_list_content():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "What is Python?"}]},
    ]
    text = _fmt_transcript(messages)
    assert "What is Python?" in text


def test_fmt_transcript_empty():
    assert _fmt_transcript([]) == ""


# ---------------------------------------------------------------------------
# FastAgentMemoryAgent — unit tests (mock runner)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return TypeSchemaRegistry()


@pytest.fixture
def mock_runner():
    runner = MagicMock()
    runner._app = MagicMock()
    runner.run = AsyncMock(return_value=json.dumps({
        "extractions": [
            {
                "action": "create",
                "type": "preference",
                "claim": "User prefers Python over JavaScript",
                "contrastive": "Python over JavaScript because of readability",
                "implied": False,
                "confidence": 0.9,
                "surprise_score": 0.0,
                "body": "",
                "target_id": None,
                "supersedes_id": None,
            }
        ],
        "skip_reason": None,
    }))
    return runner


async def test_extract_from_conversation_uses_runner(mock_runner, registry):
    agent = FastAgentMemoryAgent(model="generic.test-model")

    with patch("pyclawops.memory.vault.agent.FastAgentMemoryAgent._get_runner", return_value=mock_runner):
        result = await agent.extract_from_conversation(
            agent_id="test-agent",
            session_id="sess-001",
            messages=[
                {"role": "user", "content": "I prefer Python over JavaScript"},
                {"role": "assistant", "content": "Got it, noted."},
            ],
            existing_facts=[],
            registry=registry,
        )

    assert len(result.extractions) == 1
    assert result.extractions[0].action == ExtractionAction.CREATE
    assert result.extractions[0].fact_fields["type"] == "preference"
    mock_runner.run.assert_awaited_once()


async def test_extract_from_document_uses_runner(mock_runner, registry):
    agent = FastAgentMemoryAgent(model="generic.test-model")

    with patch("pyclawops.memory.vault.agent.FastAgentMemoryAgent._get_runner", return_value=mock_runner):
        result = await agent.extract_from_document(
            agent_id="test-agent",
            document_path="/memory/notes.md",
            document_content="# Notes\n\nWe decided to use PostgreSQL for the main database.",
            existing_facts=[],
            registry=registry,
        )

    assert len(result.extractions) == 1
    mock_runner.run.assert_awaited_once()
    # Verify document path appears in the prompt
    call_args = mock_runner.run.call_args[0][0]
    assert "/memory/notes.md" in call_args


async def test_extract_passes_existing_facts_in_prompt(registry):
    existing = [VaultFact(claim="User uses vim", type="preference")]
    captured_prompt = []

    async def _fake_run(prompt):
        captured_prompt.append(prompt)
        return json.dumps({"extractions": [], "skip_reason": "no new facts"})

    mock_r = MagicMock()
    mock_r._app = MagicMock()
    mock_r.run = AsyncMock(side_effect=_fake_run)

    agent = FastAgentMemoryAgent(model="generic.test-model")
    with patch("pyclawops.memory.vault.agent.FastAgentMemoryAgent._get_runner", return_value=mock_r):
        await agent.extract_from_conversation(
            agent_id="a",
            session_id="s",
            messages=[{"role": "user", "content": "Hi"}],
            existing_facts=existing,
            registry=registry,
        )

    assert captured_prompt
    assert "User uses vim" in captured_prompt[0]


async def test_extract_skip_reason_propagated(mock_runner, registry):
    mock_runner.run = AsyncMock(return_value=json.dumps({
        "extractions": [],
        "skip_reason": "debugging session with no durable facts",
    }))

    agent = FastAgentMemoryAgent(model="generic.test-model")
    with patch("pyclawops.memory.vault.agent.FastAgentMemoryAgent._get_runner", return_value=mock_runner):
        result = await agent.extract_from_conversation(
            agent_id="a", session_id="s",
            messages=[{"role": "user", "content": "why is this broken"}],
            existing_facts=[], registry=registry,
        )

    assert result.skip_reason == "debugging session with no durable facts"
    assert result.extractions == []


async def test_cleanup_calls_runner_cleanup():
    agent = FastAgentMemoryAgent(model="generic.test-model")
    mock_r = MagicMock()
    mock_r.cleanup = AsyncMock()
    agent._runner = mock_r

    await agent.cleanup()

    mock_r.cleanup.assert_awaited_once()
    assert agent._runner is None


async def test_cleanup_noop_when_no_runner():
    agent = FastAgentMemoryAgent(model="generic.test-model")
    # Should not raise
    await agent.cleanup()


# ---------------------------------------------------------------------------
# Integration test — skipped unless model env vars are present
# ---------------------------------------------------------------------------

_HAVE_MODEL = bool(
    os.environ.get("GENERIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
)


@pytest.mark.skipif(not _HAVE_MODEL, reason="No model credentials in environment")
async def test_integration_extract_conversation():
    """Real LLM call — skipped unless provider credentials are set."""
    import os

    if os.environ.get("GENERIC_API_KEY"):
        model = os.environ.get("VAULT_TEST_MODEL", "generic.MiniMax-M2.5")
    else:
        model = "anthropic.claude-haiku-4-5"

    registry = TypeSchemaRegistry()
    agent = FastAgentMemoryAgent(model=model, max_tokens=1024)
    try:
        result = await agent.extract_from_conversation(
            agent_id="integration-test",
            session_id="test-session-001",
            messages=[
                {"role": "user", "content": "I always prefer tabs over spaces in Python."},
                {"role": "assistant", "content": "Noted, I'll use tabs in all Python files."},
                {"role": "user", "content": "Also, we decided to use PostgreSQL for this project."},
                {"role": "assistant", "content": "Got it — PostgreSQL it is."},
            ],
            existing_facts=[],
            registry=registry,
        )
        # Should extract at least one fact
        assert isinstance(result, ExtractionResult)
        assert result.skip_reason != "parse_error", f"Parse failed: agent returned unparseable response"
        # Expect at least one extraction (tabs preference or PostgreSQL decision)
        assert len(result.extractions) >= 1
        for e in result.extractions:
            assert e.action in ExtractionAction
            assert e.fact_fields.get("claim"), "Extracted fact has no claim"
            assert 0.0 <= e.fact_fields.get("confidence", 0) <= 1.0
    finally:
        await agent.cleanup()
