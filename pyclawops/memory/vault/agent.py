"""Memory agent interface and implementations.

Defines the abstract MemoryAgent interface and the FastAgentMemoryAgent
implementation that uses an AgentRunner (FastAgent) for extraction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from .models import (
    ExtractionAction,
    ExtractionResult,
    FactExtraction,
    VaultFact,
)
from .registry import TypeSchemaRegistry

logger = logging.getLogger("pyclawops.memory.vault")

# ---------------------------------------------------------------------------
# Extraction prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a vault memory extraction agent. Your job is to identify facts worth \
storing permanently about the USER from conversations or documents. \
Return ONLY a raw JSON object — no markdown, no explanation, no code blocks.

## Memory types
{type_list}

## WORTHINESS — be ruthless
Only extract a fact if ALL of these are true:
1. A human user stated or clearly implied it (not the agent).
2. It will still be true and relevant in 6+ months.
3. It reveals something meaningful and specific about the user: a preference, \
a decision, a goal, a correction, a stable architectural/technology choice, \
a personal constraint, or a behavioral mandate the user has established.

REJECT if any of the following apply:
- Automated/machine content: cron outputs, pulse checks, system logs, runtime \
context injections (<memory> blocks), anything without a human actively speaking.
- Ephemeral data: specific counts, percentages, file paths, "X as of [date]", \
one-off script locations, log lines, stack traces, error messages.
- General how-to knowledge, facts about technology that aren't specific to this \
user (e.g. "Python uses indentation" — skip; "Jon always uses 4-space indentation" — keep).
- Temporary workarounds, debugging steps, installation instructions.
- Pleasantries, greetings, small talk, single-word acknowledgments, filler.
- Anything the agent said or did — only facts ABOUT THE USER matter.
- Vague statements without a clear subject-predicate-object (e.g. "things are going well").

## TYPE SELECTION — choose carefully

**user** — Personal identity/background facts about the user themselves. \
Job, role, location, experience, skills. \
"Jon is a backend engineer with 10 years of Python experience." \
Use this for WHO the user IS, not what they prefer or decided.

**preference** — A stable personal preference about tools, style, or working method. \
"Prefers uv over pip", "likes concise replies without summaries". \
Not a one-time choice — a lasting inclination.

**decision** — A specific choice made for a project or initiative. \
"Decided to use PostgreSQL for AZDB", "Going with FastAPI for the API layer." \
Tied to a named project or context. Use `part_of` wikilink to associate with a project.

**fact** — A non-personal contextual fact that doesn't fit other types. \
Use sparingly — most things fit a more specific type.

**lesson** — Something the user or team learned the hard way, with lasting impact. \
"Mocked tests passed but prod migration failed — always use a real DB in tests."

**commitment** — An explicit future action the user committed to. \
"Will migrate off Redis by end of Q2."

**goal** — A longer-horizon objective the user is working toward. \
"Building a self-hosted AI stack", "Planning to open-source the AZDB project." \
Distinct from commitment (which is a concrete near-term action).

**project** — An ongoing named project. Acts as an anchor for related facts. \
"AZDB: a distributed key-value store project." Other facts link here via \
`[[part_of::AZDB project]]`. Keep the claim to the project name + one-line description.

**context** — Environment or setup facts: OS, hardware, dev tools, language versions. \
"Runs macOS on Apple Silicon M3", "Primary editor is Neovim", \
"Python 3.12 on all dev machines." Stable but changes occasionally.

**person** — Info about a real person in the user's orbit. \
"Alex is the on-call lead for infrastructure at Acme."

**hypothesis** — Tentative or unconfirmed belief, low confidence (<0.6). \
"Thinks the auth middleware may be causing the latency — unconfirmed."

**absence** — Confirmed non-existence or deliberate non-use. \
"Does not use Windows", "No Kubernetes in the stack."

**anti** — Explicitly rejected option, often with stated reasoning. \
"Will NOT use MongoDB — relational queries were too painful."

**rule** — A behavioral constraint or mandate the user has imposed on the agent. \
"Always check schema before editing config files", "Never delete without confirmation." \
Rules govern the AGENT's behavior, not facts about the user. \
NEVER auto-injected — the agent queries these explicitly. \
Use this ONLY when the user is explicitly telling the agent how to behave, \
not when describing their own preferences or workflows.

## CLASSIFICATION EXAMPLES

User: "Jon requires the agent to check schemas before editing config files."
→ type=rule, claim="Check schemas before editing config files", confidence=0.95
(Governs agent behavior — NOT a user fact or preference)

User: "I prefer 4-space indentation in all my projects"
→ type=preference, claim="Prefers 4-space indentation", confidence=0.85

User: "We decided to use Redis for caching in the AZDB project"
→ type=decision, claim="Redis used for caching in AZDB project", confidence=0.9,
  body="[[part_of::AZDB project]]"

User: "I'm a senior backend engineer, been doing this for 12 years"
→ type=user, claim="Senior backend engineer with 12 years of experience", confidence=0.9

User: "I run everything on macOS, M3 MacBook Pro"
→ type=context, claim="Runs macOS on Apple Silicon M3 MacBook Pro", confidence=0.9

User: "I'm building AZDB — a distributed key-value store for embedded use"
→ type=project, claim="AZDB: distributed key-value store for embedded use", confidence=0.85

User: "My goal is to get the whole thing open-sourced by end of year"
→ type=goal, claim="Plans to open-source AZDB by end of year", confidence=0.75

User: "I manage a fleet of Raspberry Pis"
→ type=context, claim="Manages a fleet of Raspberry Pis", confidence=0.75

User: "Don't remind me about that bug again, it's fixed"
→ SKIP — ephemeral, conversational

User: "I'll add logging tomorrow"
→ type=commitment, claim="Will add logging to the project", confidence=0.6

User: "We got burned last quarter — never mock the database again"
→ type=lesson, claim="Never mock the database in tests — real-DB failures were masked",
  confidence=0.9, surprise_score=0.8

## Reinforce/supersede
If a new observation reinforces an existing fact: action="reinforce", target_id=<id>.
If new info contradicts an existing fact: action="supersede", supersedes_id=<id>.
Check the existing facts list carefully — do NOT create duplicates.

## Wikilinks (optional body field only)
Plain: [[target claim]] → generic similarity (related_to)
Typed:
  [[depends_on::target]]  — this fact requires target to be true
  [[part_of::target]]     — this is part of a larger fact/project
  [[contradicts::target]] — this negates target (triggers auto-supersession)
Only add links in "body", never in "claim". Only typed links when unambiguous.

## Output format
{{"extractions": [{{"action": "create", "type": "<type>", "claim": "<fact>", \
"contrastive": null, "implied": false, "confidence": 0.9, "surprise_score": 0.0, \
"body": null, "target_id": null, "supersedes_id": null}}], "skip_reason": null}}

If nothing worthy to extract: {{"extractions": [], "skip_reason": "<reason>"}}
"""

_CONV_PROMPT_TEMPLATE = """\
Extract memory facts from this conversation segment.

Agent: {agent_id}
Session: {session_id}

{existing_facts_block}

## Conversation

{transcript}

Return ONLY the JSON response as described in your instructions.\
"""

_DOC_PROMPT_TEMPLATE = """\
Extract memory facts from this document.

Agent: {agent_id}
Document: {document_path}

{existing_facts_block}

## Document content

{content}

Return ONLY the JSON response as described in your instructions.\
"""


def _fmt_existing_facts(facts: list[VaultFact]) -> str:
    if not facts:
        return ""
    # Build lookup for resolving typed link claims
    fact_map = {f.id: f for f in facts}
    lines = ["## Existing facts (check for reinforce/supersede)\n"]
    for f in facts[:30]:  # cap at 30 to avoid excessive context
        line = f"- [{f.id}] ({f.type}) {f.claim}"
        if f.contrastive:
            line += f" — {f.contrastive}"
        lines.append(line)
        # Show typed links when present so LLM can reference them
        for dep_id in f.depends_on[:2]:
            dep = fact_map.get(dep_id)
            if dep:
                lines.append(f"    depends_on: [{dep_id}] {dep.claim[:60]}")
        if f.part_of:
            parent = fact_map.get(f.part_of)
            if parent:
                lines.append(f"    part_of: [{f.part_of}] {parent.claim[:60]}")
        for con_id in f.contradicts[:2]:
            con = fact_map.get(con_id)
            if con:
                lines.append(f"    contradicts: [{con_id}] {con.claim[:60]}")
    return "\n".join(lines) + "\n"


def _fmt_transcript(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        if isinstance(content, list):
            # FA message format with content blocks
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _try_parse_json(text: str) -> Optional[dict]:
    """Try increasingly aggressive strategies to parse a JSON object from text."""
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract first {...} block (handles preamble/postamble)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Truncated JSON recovery: if text starts with { but is cut off,
    # try to salvage any complete extraction objects already present
    if text.lstrip().startswith("{"):
        # Extract all complete {...} objects inside "extractions": [...]
        items = re.findall(r'\{[^{}]*"action"[^{}]*\}', text, re.DOTALL)
        if items:
            extractions = []
            for item in items:
                try:
                    extractions.append(json.loads(item))
                except json.JSONDecodeError:
                    pass
            if extractions:
                return {"extractions": extractions, "skip_reason": None}

    return None


def _parse_extraction_response(text: str) -> ExtractionResult:
    """Parse a JSON extraction response from the memory agent."""
    text = text.strip()

    # Strip markdown code fences if the model wrapped the output
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()

    data = _try_parse_json(text)

    if data is None:
        logger.warning("Memory agent returned unparseable response: %s", text[:200])
        return ExtractionResult(skip_reason="parse_error")

    extractions: list[FactExtraction] = []
    for item in data.get("extractions") or []:
        action_str = item.get("action", "create")
        try:
            action = ExtractionAction(action_str)
        except ValueError:
            action = ExtractionAction.CREATE

        fact_fields: dict[str, Any] = {
            "type": item.get("type", "fact"),
            "claim": item.get("claim", ""),
            "confidence": float(item.get("confidence", 0.7)),
            "surprise_score": float(item.get("surprise_score", 0.0)),
            "implied": bool(item.get("implied", False)),
        }
        if item.get("contrastive"):
            fact_fields["contrastive"] = item["contrastive"]
        if item.get("body"):
            fact_fields["body"] = item["body"]

        extractions.append(FactExtraction(
            action=action,
            fact_fields=fact_fields,
            target_id=item.get("target_id") or None,
            supersedes_id=item.get("supersedes_id") or None,
        ))

    return ExtractionResult(
        extractions=extractions,
        skip_reason=data.get("skip_reason") or None,
    )


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class MemoryAgent(ABC):
    """Abstract interface for the memory extraction agent."""

    @abstractmethod
    async def extract_from_conversation(
        self,
        agent_id: str,
        session_id: str,
        messages: list[dict],
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        """Extract memory facts from a conversation segment."""

    @abstractmethod
    async def extract_from_document(
        self,
        agent_id: str,
        document_path: str,
        document_content: str,
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        """Extract memory facts from a document file."""

    async def cleanup(self) -> None:
        """Release any resources held by this agent. Override if needed."""


# ---------------------------------------------------------------------------
# FastAgent implementation
# ---------------------------------------------------------------------------


class FastAgentMemoryAgent(MemoryAgent):
    """Memory agent backed by an AgentRunner (FastAgent).

    Uses an AgentRunner with no MCP servers and use_history=False so every
    extraction call is stateless.  The model string is in pyclawops format
    (e.g. ``"minimax/MiniMax-M2.5"``) and is translated to a FastAgent
    model string via ``_translate_to_fa_model``.

    For isolated testing without a full gateway, pass the FA model string
    directly (e.g. ``"generic.MiniMax-M2.5"``) and ensure the provider env
    vars (``GENERIC_API_KEY``, ``GENERIC_BASE_URL``) are set.

    Args:
        model: Model string in pyclawops format or FA format.
        pyclawops_config: Loaded pyclawops Config object (used for provider
            credential lookup).  May be None when testing in isolation.
        max_tokens: Max output tokens for extraction responses.
        temperature: Sampling temperature (low = more consistent).
    """

    def __init__(
        self,
        model: str,
        pyclawops_config: Optional[Any] = None,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> None:
        self._raw_model = model
        self._pyclawops_config = pyclawops_config
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._runner: Optional[Any] = None  # AgentRunner, lazily created

    def _get_runner(self) -> Any:
        if self._runner is None:
            from pyclawops.core.agent import _get_provider_cfg, _translate_to_fa_model
            from pyclawops.agents.runner import AgentRunner

            fa_model = _translate_to_fa_model(self._raw_model, self._pyclawops_config)
            logger.debug("Memory agent using FA model: %s", fa_model)

            # Extract provider credentials so the runner uses the correct endpoint
            api_key = None
            base_url = None
            if "/" in self._raw_model:
                provider_name = self._raw_model.split("/", 1)[0]
                pcfg = _get_provider_cfg(self._pyclawops_config, provider_name)
                if pcfg:
                    api_key = getattr(pcfg, "api_key", None)
                    base_url = getattr(pcfg, "api_url", None) or getattr(pcfg, "base_url", None)

            self._runner = AgentRunner(
                agent_name="memory-agent",
                instruction="",  # system prompt injected per-call via the prompt
                model=fa_model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                servers=[],  # no MCP servers — pure LLM extraction
                request_params={
                    "use_history": False,
                },
                api_key=api_key,
                base_url=base_url,
                pyclawops_config=self._pyclawops_config,
                priority="background",  # vault extraction is low-priority
            )
        return self._runner

    async def cleanup(self) -> None:
        """Close the underlying AgentRunner and release MCP connections."""
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    async def _call(self, system: str, prompt: str) -> ExtractionResult:
        runner = self._get_runner()
        if runner._app is None:
            await runner.initialize()
        # Prepend the system prompt inside the user message since AgentRunner
        # uses `instruction` as the system prompt but we want a fresh one per call.
        full_prompt = f"{system}\n\n---\n\n{prompt}"
        for attempt in range(2):
            response = await runner.run(full_prompt)
            result = _parse_extraction_response(response)
            if result.skip_reason != "parse_error":
                return result
            if attempt == 0:
                logger.debug("Parse error on attempt 1, retrying…")
        return result

    async def extract_from_conversation(
        self,
        agent_id: str,
        session_id: str,
        messages: list[dict],
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        system = _SYSTEM_PROMPT.format(type_list=registry.memory_agent_type_list())
        prompt = _CONV_PROMPT_TEMPLATE.format(
            agent_id=agent_id,
            session_id=session_id,
            existing_facts_block=_fmt_existing_facts(existing_facts),
            transcript=_fmt_transcript(messages),
        )
        return await self._call(system, prompt)

    async def extract_from_document(
        self,
        agent_id: str,
        document_path: str,
        document_content: str,
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        system = _SYSTEM_PROMPT.format(type_list=registry.memory_agent_type_list())
        prompt = _DOC_PROMPT_TEMPLATE.format(
            agent_id=agent_id,
            document_path=document_path,
            existing_facts_block=_fmt_existing_facts(existing_facts),
            content=document_content[:8000],  # cap to avoid oversize prompts
        )
        return await self._call(system, prompt)

    async def cleanup(self) -> None:
        """Close the underlying AgentRunner and release FA connections."""
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._runner = None


# ---------------------------------------------------------------------------
# Regex-based implementation (no LLM required)
# ---------------------------------------------------------------------------


class RegexMemoryAgent(MemoryAgent):
    """Deterministic pattern-based memory agent. No LLM required.

    Scans USER messages in a conversation for recognisable patterns and emits
    typed FactExtraction objects.  Only USER turns are scanned — agent turns
    are ignored.  Document extraction is not supported (returns empty).

    Fact types detected:
    - preference  — "I prefer / always use / never use / I like / I don't like"
    - decision    — "decided / going with / we'll use / chose"
    - correction  — messages starting with "actually / no, / wait, / correction"
    - commitment  — "I'll / I will / going to / planning to" + action verb
    - fact        — "I am / I work / I use / I manage / I build" statements
    """

    # (type, confidence, compiled patterns)
    _RULES: list[tuple[str, float, list[re.Pattern]]] = [
        ("preference", 0.75, [
            re.compile(r"\bI\s+(?:always|never)\s+use\b", re.I),
            re.compile(r"\bI\s+(?:prefer|like\s+to|love\s+using|hate\s+using|dislike)\b", re.I),
            re.compile(r"\bmy\s+(?:preference|go-to|default)\b", re.I),
            re.compile(r"\bI\s+don't\s+(?:use|like|want|do)\b", re.I),
        ]),
        ("decision", 0.80, [
            re.compile(r"\b(?:we\s+)?(?:decided|chose|going\s+with|picked)\b", re.I),
            re.compile(r"\bwe'?ll\s+use\b", re.I),
            re.compile(r"\bthe\s+(?:decision|choice)\s+is\b", re.I),
        ]),
        ("correction", 0.85, [
            re.compile(r"^(?:actually|no[,\s]|wait[,\s]|correction[,:]|that'?s\s+(?:wrong|incorrect|not\s+right))", re.I),
        ]),
        ("commitment", 0.70, [
            re.compile(r"\bI'?(?:ll| will)\b.{1,60}\b(?:do|make|create|build|fix|add|update|send|write|implement|deploy|finish)\b", re.I),
            re.compile(r"\b(?:I'?m\s+)?(?:going\s+to|planning\s+to)\b.{1,60}\b(?:do|make|create|build|fix|add|update|send|write|implement|deploy|finish)\b", re.I),
        ]),
        ("fact", 0.55, [
            re.compile(r"\bI\s+(?:am\s+a|work\s+(?:at|for|on)|own\s+a|run\s+a|manage\s+a|build|use\s+[A-Z])\b", re.I),
        ]),
    ]

    def _extract_from_text(self, text: str) -> list[FactExtraction]:
        results: list[FactExtraction] = []
        # Scan line by line — each line is a potential claim
        for line in text.splitlines():
            line = line.strip()
            if len(line) < 10 or len(line) > 300:
                continue
            for fact_type, confidence, patterns in self._RULES:
                for pat in patterns:
                    if pat.search(line):
                        results.append(FactExtraction(
                            action=ExtractionAction.CREATE,
                            fact_fields={
                                "type": fact_type,
                                "claim": line,
                                "confidence": confidence,
                                "surprise_score": 0.0,
                                "implied": False,
                            },
                        ))
                        break  # one match per line per rule group is enough
                else:
                    continue
                break  # stop checking rule groups once one matched
        return results

    async def extract_from_conversation(
        self,
        agent_id: str,
        session_id: str,
        messages: list[dict],
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        extractions: list[FactExtraction] = []
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            extractions.extend(self._extract_from_text(content))
        return ExtractionResult(
            extractions=extractions,
            skip_reason=None if extractions else "no_patterns_matched",
        )

    async def extract_from_document(
        self,
        agent_id: str,
        document_path: str,
        document_content: str,
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        # Regex extraction on unstructured documents is too noisy — skip.
        return ExtractionResult(extractions=[], skip_reason="regex_agent_skips_documents")


# ---------------------------------------------------------------------------
# Mock implementation for tests
# ---------------------------------------------------------------------------


class MockMemoryAgent(MemoryAgent):
    """Test-friendly mock memory agent. Returns pre-configured results."""

    def __init__(self, results: Optional[list[ExtractionResult]] = None) -> None:
        self._results = iter(results or [])
        self._calls: list[dict] = []

    async def extract_from_conversation(
        self,
        agent_id: str,
        session_id: str,
        messages: list[dict],
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        self._calls.append({
            "type": "conversation",
            "agent_id": agent_id,
            "session_id": session_id,
            "message_count": len(messages),
            "existing_count": len(existing_facts),
        })
        return next(self._results, ExtractionResult(extractions=[], skip_reason="mock"))

    async def extract_from_document(
        self,
        agent_id: str,
        document_path: str,
        document_content: str,
        existing_facts: list[VaultFact],
        registry: TypeSchemaRegistry,
    ) -> ExtractionResult:
        self._calls.append({
            "type": "document",
            "agent_id": agent_id,
            "document_path": document_path,
            "content_length": len(document_content),
            "existing_count": len(existing_facts),
        })
        return next(self._results, ExtractionResult(extractions=[], skip_reason="mock"))

    @property
    def calls(self) -> list[dict]:
        """Return the list of all calls made to this mock agent."""
        return self._calls
