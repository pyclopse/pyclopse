"""Agent runner using FastAgent."""
import asyncio
from pyclaw.reflect import reflect_system
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

# Regex to strip <thinking>...</thinking> blocks (case-insensitive, dotall)
_THINKING_RE = re.compile(r"<(thinking|think)>(.*?)</(thinking|think)>", re.DOTALL | re.IGNORECASE)

# Applied once when the first generic-provider runner initialises.
_reasoning_details_patched: bool = False


def _patch_openai_llm_for_reasoning_details() -> None:
    """Patch OpenAILLM once so delta.reasoning_details is emitted as is_reasoning=True chunks.

    MiniMax (and potentially other providers) put thinking content in
    ``delta.reasoning_details`` (a cumulative list) rather than in
    ``delta.reasoning_content``.  FastAgent ignores this field entirely, so we
    add support here by wrapping ``_process_stream_chunk_common`` at the class
    level — no per-stream setup required.

    Per-instance state (``self._reasoning_details_buf``) tracks the cumulative
    text seen so far.  A shorter-or-mismatched value signals a new stream
    session and resets the buffer automatically.
    """
    global _reasoning_details_patched
    if _reasoning_details_patched:
        return
    try:
        from fast_agent.llm.provider.openai.llm_openai import OpenAILLM
        from fast_agent.llm.stream_types import StreamChunk

        _original = OpenAILLM._process_stream_chunk_common

        def _patched(self, chunk, **kw):  # noqa: ANN001
            if chunk.choices:
                delta = chunk.choices[0].delta
                for detail in getattr(delta, "reasoning_details", None) or []:
                    if isinstance(detail, dict) and "text" in detail:
                        buf: str = getattr(self, "_reasoning_details_buf", "")
                        new_text: str = detail["text"]
                        # Detect a new stream session: cumulative text restarted
                        if not new_text.startswith(buf):
                            buf = ""
                        incremental = new_text[len(buf):]
                        self._reasoning_details_buf = new_text
                        if incremental:
                            self._notify_stream_listeners(
                                StreamChunk(text=incremental, is_reasoning=True)
                            )
            return _original(self, chunk, **kw)

        OpenAILLM._process_stream_chunk_common = _patched
        _reasoning_details_patched = True
        logging.getLogger(__name__).debug(
            "Patched OpenAILLM._process_stream_chunk_common for delta.reasoning_details"
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            f"Could not patch OpenAILLM for delta.reasoning_details: {exc}"
        )


def strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> blocks from text and normalise whitespace.

    Args:
        text (str): Input text that may contain thinking/think XML tags.

    Returns:
        str: Text with all thinking blocks removed and excess blank lines collapsed.
    """
    stripped = _THINKING_RE.sub("", text)
    # Collapse more than two consecutive newlines left behind by removals
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def format_thinking_for_telegram(text: str) -> Optional[str]:
    """Return a single HTML-formatted Telegram message with thinking shown as an inline spoiler.

    The thinking block is hidden behind a tap-to-reveal spoiler; the response
    follows immediately after so everything arrives in one message.

    Args:
        text (str): Raw agent response that may contain thinking/think XML tags.

    Returns:
        Optional[str]: HTML-formatted string with an expandable blockquote for
        thinking content, or None if the text contains no thinking blocks.
    """
    import html as _html

    matches = list(_THINKING_RE.finditer(text))
    if not matches:
        return None

    # Collect all thinking sections
    thinking_content = "\n\n".join(m.group(2).strip() for m in matches)
    response = strip_thinking_tags(text)

    # quote=False: only escape &, <, > — Telegram's HTML parser does not
    # support &quot; in text content and will truncate at the first one.
    safe_thinking = _html.escape(thinking_content, quote=False)
    safe_response = _html.escape(response, quote=False)

    return f"<blockquote expandable><i>💭 {safe_thinking}</i></blockquote>\n\n{safe_response}"

logger = logging.getLogger(__name__)

# stop_reason values that indicate an incomplete / broken assistant turn.
# Saving history that ends with one of these causes a one-turn-lag bug with
# MiniMax (and possibly other providers) on the next reload: FastAgent's
# reconcile_interrupted_history appends a fake tool result, and the subsequent
# real tool result ends up referencing the stale tool_call_id.
_INCOMPLETE_STOP_REASONS = frozenset({"toolUse", "error", "cancelled", "timeout"})

# FastAgent catches model errors and returns them as plain text instead of raising.
# We detect these strings so we can raise instead of saving them to history.
_FA_ERROR_PREFIX = "I hit an internal error"


def _is_fastagent_error_msg(msg: Any) -> bool:
    """Return True if msg is a FastAgent internal-error assistant message.

    FastAgent catches model errors and returns them as a plain-text assistant
    message starting with ``_FA_ERROR_PREFIX`` rather than raising an exception.
    This helper detects that pattern so callers can raise instead of saving the
    error response to history.

    Args:
        msg (Any): A FastAgent ``PromptMessageExtended`` or similar message object.

    Returns:
        bool: True if the message is an assistant error response, False otherwise.
    """
    if getattr(msg, "role", None) != "assistant":
        return False
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content.startswith(_FA_ERROR_PREFIX)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if text and isinstance(text, str) and text.startswith(_FA_ERROR_PREFIX):
                return True
    return False


def _purge_corrupted_pairs(messages: List[Any]) -> List[Any]:
    """Remove corrupted ``assistant(stop=toolUse, no tool_use content) + user(empty)``
    pairs from *anywhere* in the message list.

    These pairs are injected by FastAgent's ``reconcile_interrupted_history`` when
    a previous session was interrupted mid-tool-call.  They leave orphaned
    assistant messages whose stop_reason claims a tool call was made but whose
    content has no ``tool_use`` blocks — followed by an empty user turn.  Sending
    this sequence to MiniMax (and other providers) triggers "tool call result does
    not follow tool call (2013)".

    Unlike ``_trim_history_for_save`` (which only cleans the tail), this pass
    removes these pairs from anywhere in the history before it is written to disk,
    preventing them from accumulating under later messages.
    """
    if not messages:
        return messages

    def _has_tool_calls(msg: Any) -> bool:
        """Return True if the message has actual tool calls in msg.tool_calls.

        Args:
            msg (Any): A FastAgent message object.

        Returns:
            bool: True if tool_calls is non-None and non-empty.
        """
        tool_calls = getattr(msg, "tool_calls", None)
        return bool(tool_calls)  # non-None, non-empty dict

    def _is_empty_user(msg: Any) -> bool:
        """Return True only if the user message has NO content AND NO tool_results.

        Legitimate tool-result user messages have content=[] (empty list) but DO
        have tool_results populated.  Only messages with both empty are orphaned.

        Args:
            msg (Any): A FastAgent message object.

        Returns:
            bool: True if the message is a user role message with no content and
            no tool_results (i.e. an orphaned synthetic placeholder).
        """
        if getattr(msg, "role", None) != "user":
            return False
        content = getattr(msg, "content", None)
        tool_results = getattr(msg, "tool_results", None)
        return (not content) and (not tool_results)

    cleaned: List[Any] = []
    skip_next = False
    for i, msg in enumerate(messages):
        if skip_next:
            skip_next = False
            continue
        role = getattr(msg, "role", None)
        stop = getattr(msg, "stop_reason", None)
        stop_val = stop.value if hasattr(stop, "value") else (str(stop) if stop else None)
        # Corrupted assistant: claims toolUse but has no actual tool_calls,
        # followed immediately by a user turn with no content AND no tool_results.
        if (
            role == "assistant"
            and stop_val in _INCOMPLETE_STOP_REASONS
            and not _has_tool_calls(msg)
            and i + 1 < len(messages)
            and _is_empty_user(messages[i + 1])
        ):
            skip_next = True
            continue
        cleaned.append(msg)
    return cleaned


def _trim_history_for_save(messages: List[Any]) -> List[Any]:
    """Return a copy of *messages* with any trailing incomplete assistant turns removed.

    A trailing ``assistant`` message whose ``stop_reason`` is one of the
    incomplete values (toolUse, error, cancelled, timeout) indicates the
    agentic loop was interrupted before the full exchange finished.  Saving
    such state and then reloading it triggers
    ``reconcile_interrupted_history``, which inserts a fake tool-result whose
    ID no longer matches the IDs in the next real tool call, causing MiniMax's
    "tool call id is invalid (2013)" error.

    We trim these messages off so the persisted history always ends at the last
    *complete* exchange, keeping the conversation context intact.
    """
    if not messages:
        return []
    trimmed = list(messages)
    while trimmed:
        last = trimmed[-1]
        role = getattr(last, "role", None)
        stop = getattr(last, "stop_reason", None)
        stop_val = stop.value if hasattr(stop, "value") else (str(stop) if stop else None)

        if role == "assistant":
            if stop_val in _INCOMPLETE_STOP_REASONS:
                trimmed.pop()
            else:
                break
        elif role == "user":
            # An empty user message that immediately follows an incomplete assistant
            # turn is a fake tool-result placeholder injected by FastAgent's
            # reconcile_interrupted_history.  Trim it so we can then trim the
            # preceding incomplete assistant turn on the next iteration.
            content = getattr(last, "content", None)
            content_empty = not content  # None, [], or ""
            if content_empty and len(trimmed) >= 2:
                prev = trimmed[-2]
                prev_stop = getattr(prev, "stop_reason", None)
                prev_stop_val = (
                    prev_stop.value if hasattr(prev_stop, "value")
                    else (str(prev_stop) if prev_stop else None)
                )
                if (
                    getattr(prev, "role", None) == "assistant"
                    and prev_stop_val in _INCOMPLETE_STOP_REASONS
                ):
                    trimmed.pop()
                    continue
            break
        else:
            break
    return trimmed


def _strip_tool_machinery(messages: List[Any]) -> List[Any]:
    """Return a copy of *messages* with all tool-call/result plumbing removed.

    Only user text and assistant text turns are kept — tool call requests and
    tool result responses are stripped before writing to disk.  This reduces
    history file size, avoids polluting the context window on reload with stale
    tool output, and produces clean text for future RAG/vector indexing.

    Rules applied per message:

    * **User messages** with ``tool_results`` populated are synthetic
      tool-result responses injected by FastAgent — dropped entirely.
    * **Assistant messages** with ``tool_calls`` but no ``content`` are
      pure tool-dispatch turns with no human-visible text — dropped entirely.
    * **Assistant messages** with both ``tool_calls`` *and* ``content`` had
      a text preamble before calling tools — kept with ``tool_calls`` and
      ``stop_reason`` cleared so the saved turn looks like a normal reply.
    * All other messages are kept unchanged.

    After filtering, consecutive same-role turns (which can arise when adjacent
    tool-only turns are dropped) are merged by concatenating their content lists
    into a single message.

    This function operates on a *copy* and never mutates the live
    ``agent.message_history`` — the active session retains full tool context.
    """
    try:
        from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    except Exception:
        # FA not available (e.g. unit tests) — return unchanged
        return list(messages)

    filtered: List[Any] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        tool_calls = getattr(msg, "tool_calls", None)
        tool_results = getattr(msg, "tool_results", None)
        content = getattr(msg, "content", None) or []

        if role == "user":
            if tool_results:
                continue  # synthetic tool-result message → drop
            filtered.append(msg)

        elif role == "assistant":
            if tool_calls and not content:
                continue  # pure tool-dispatch, no text → drop
            if tool_calls:
                # text preamble + tool calls → keep text, clear tool machinery
                filtered.append(PromptMessageExtended(
                    role=msg.role,
                    content=content,
                    tool_calls=None,
                    tool_results=None,
                    stop_reason=None,
                    phase=getattr(msg, "phase", None),
                ))
            else:
                filtered.append(msg)

        else:
            filtered.append(msg)

    # Merge consecutive same-role turns produced by the drops above.
    merged: List[Any] = []
    for msg in filtered:
        if merged and getattr(merged[-1], "role", None) == getattr(msg, "role", None):
            prev = merged[-1]
            merged[-1] = PromptMessageExtended(
                role=prev.role,
                content=list(getattr(prev, "content", None) or []) + list(getattr(msg, "content", None) or []),
                tool_calls=None,
                tool_results=None,
                stop_reason=getattr(prev, "stop_reason", None),
                phase=getattr(prev, "phase", None),
            )
        else:
            merged.append(msg)

    return merged


# Default MCP servers every agent gets (can be extended per-agent).
# "pyclaw" provides all built-in tools: bash, memory, todos, sessions, etc.
_DEFAULT_SERVERS: List[str] = ["pyclaw"]

# Built-in server names that pyclaw knows how to configure programmatically.
_BUILTIN_SERVERS = frozenset(["pyclaw", "fetch", "time", "filesystem", "chrome-devtools"])



@reflect_system("agent-runner")
class AgentRunner:
    """Runner for FastAgent-based execution.

    Wires MCP servers from agent config so tools are available to the agent.
    Each runner owns a single FastAgent ``fast.run()`` context that stays alive
    for the entire lifetime of the runner.  Session history is persisted to
    ``history_path`` on disk using FastAgent's native JSON format.

    Attributes:
        agent_name (str): Name of the agent as registered with FastAgent.
        instruction (str): System prompt / instruction for the agent.
        model (str): Model string passed to FastAgent (e.g. ``anthropic.claude-sonnet-4-6``).
        temperature (float): Sampling temperature for the model.
        max_tokens (Optional[int]): Maximum tokens per response.
        top_p (Optional[float]): Top-p nucleus sampling parameter.
        max_iterations (Optional[int]): Maximum agentic loop iterations.
        parallel_tool_calls (Optional[bool]): Whether to allow parallel tool calls.
        streaming_timeout (Optional[float]): Per-chunk streaming timeout in seconds.
        request_params (Dict[str, Any]): Extra provider-specific request parameters.
        servers (List[str]): MCP server names from fastagent.config.yaml.
        tools_config (Dict[str, Any]): Tool allowlist/denylist config.
        show_thinking (bool): If False, strip ``<thinking>`` blocks before returning.
        api_key (Optional[str]): Provider API key override.
        base_url (Optional[str]): Provider base URL override.
        owner_name (str): Agent name sent as ``X-Agent-Name`` to the MCP server.
        session_id (Optional[str]): Session ID for per-session runners; None for base runners.
        history_path (Optional[Path]): Path to ``history.json`` for session persistence.
        reasoning_effort (Optional[str]): FA reasoning effort setting.
        text_verbosity (Optional[str]): FA text verbosity setting.
        service_tier (Optional[str]): FA service tier setting.
        workflow (Optional[str]): Workflow type if this runner wraps a workflow agent.
        child_agent_configs (Dict[str, Any]): Child agent configurations for workflow runners.
        plan_type (str): Planning type for orchestrator/iterative_planner workflows.
        plan_iterations (Optional[int]): Max planning iterations.
        generator (Optional[str]): Generator agent name for evaluator_optimizer workflow.
        evaluator (Optional[str]): Evaluator agent name for evaluator_optimizer workflow.
        min_rating (str): Minimum rating for evaluator_optimizer workflow.
        max_refinements (int): Max refinement rounds for evaluator_optimizer workflow.
        refinement_instruction (Optional[str]): Extra refinement instruction.
        worker (Optional[str]): Worker agent name for maker workflow.
        k (int): Number of candidates for maker workflow K-voting.
        max_samples (int): Maximum samples for maker workflow.
        match_strategy (str): Match strategy for maker workflow.
        red_flag_max_length (Optional[int]): Max length before red-flag detection triggers.
        pyclaw_config (Optional[Any]): Full PyClaw config object for building FA settings.
    """

    def __init__(
        self,
        agent_name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        max_iterations: Optional[int] = None,
        parallel_tool_calls: Optional[bool] = None,
        # ── Workflow params ───────────────────────────────────────────────────
        # workflow: orchestrator | iterative_planner | evaluator_optimizer | maker
        # child_agent_configs: {name: {instruction, model, servers, max_tokens}}
        workflow: Optional[str] = None,
        child_agent_configs: Optional[Dict[str, Any]] = None,
        # orchestrator / iterative_planner
        plan_type: str = "full",
        plan_iterations: Optional[int] = None,
        # evaluator_optimizer
        generator: Optional[str] = None,
        evaluator: Optional[str] = None,
        min_rating: str = "GOOD",
        max_refinements: int = 3,
        refinement_instruction: Optional[str] = None,
        # maker (K-voting)
        worker: Optional[str] = None,
        k: int = 3,
        max_samples: int = 50,
        match_strategy: str = "exact",
        red_flag_max_length: Optional[int] = None,
        streaming_timeout: Optional[float] = None,
        servers: Optional[List[str]] = None,
        tools_config: Optional[Dict[str, Any]] = None,
        show_thinking: bool = False,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        owner_name: Optional[str] = None,
        request_params: Optional[Dict[str, Any]] = None,
        history_path: Optional[Path] = None,
        session_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        text_verbosity: Optional[str] = None,
        service_tier: Optional[str] = None,
        pyclaw_config: Optional[Any] = None,
        priority: str = "critical",
    ):
        """Initialize an AgentRunner with all agent and workflow configuration.

        Args:
            agent_name (str): Name of the agent registered with FastAgent.
            instruction (str): System prompt for the agent.
            model (str): Model string, e.g. ``anthropic.claude-sonnet-4-6``. Defaults to ``"sonnet"``.
            temperature (float): Sampling temperature. Defaults to 0.7.
            max_tokens (Optional[int]): Maximum tokens per response. Defaults to None.
            top_p (Optional[float]): Top-p nucleus sampling value. Defaults to None.
            max_iterations (Optional[int]): Max agentic loop iterations. Defaults to None.
            parallel_tool_calls (Optional[bool]): Whether parallel tool calls are allowed. Defaults to None.
            workflow (Optional[str]): Workflow type: ``orchestrator``, ``iterative_planner``,
                ``evaluator_optimizer``, or ``maker``. Defaults to None.
            child_agent_configs (Optional[Dict[str, Any]]): Child agent configurations for
                workflow runners keyed by agent name. Defaults to None.
            plan_type (str): Planning type for orchestrator/iterative_planner. Defaults to ``"full"``.
            plan_iterations (Optional[int]): Max planning iterations. Defaults to None.
            generator (Optional[str]): Generator agent name for evaluator_optimizer. Defaults to None.
            evaluator (Optional[str]): Evaluator agent name for evaluator_optimizer. Defaults to None.
            min_rating (str): Minimum rating for evaluator_optimizer. Defaults to ``"GOOD"``.
            max_refinements (int): Max refinement rounds for evaluator_optimizer. Defaults to 3.
            refinement_instruction (Optional[str]): Extra refinement instruction. Defaults to None.
            worker (Optional[str]): Worker agent name for maker workflow. Defaults to None.
            k (int): Number of K-voting candidates for maker workflow. Defaults to 3.
            max_samples (int): Maximum samples for maker workflow. Defaults to 50.
            match_strategy (str): Match strategy for maker workflow. Defaults to ``"exact"``.
            red_flag_max_length (Optional[int]): Max response length before red-flag triggers. Defaults to None.
            streaming_timeout (Optional[float]): Per-chunk streaming timeout in seconds. Defaults to None.
            servers (Optional[List[str]]): MCP server names. Defaults to ``["pyclaw"]``.
            tools_config (Optional[Dict[str, Any]]): Tool policy config (allowlist/denylist). Defaults to None.
            show_thinking (bool): If True, thinking blocks are returned as-is. Defaults to False.
            api_key (Optional[str]): Provider API key override. Defaults to None.
            base_url (Optional[str]): Provider base URL override. Defaults to None.
            owner_name (Optional[str]): Agent name sent as ``X-Agent-Name`` header. Defaults to ``agent_name``.
            request_params (Optional[Dict[str, Any]]): Extra provider-specific request parameters. Defaults to None.
            history_path (Optional[Path]): Path to ``history.json`` for session persistence. Defaults to None.
            session_id (Optional[str]): Session ID for per-session runners. Defaults to None.
            reasoning_effort (Optional[str]): FA reasoning effort setting. Defaults to None.
            text_verbosity (Optional[str]): FA text verbosity setting. Defaults to None.
            service_tier (Optional[str]): FA service tier setting. Defaults to None.
            pyclaw_config (Optional[Any]): Full PyClaw config object for building FA Settings. Defaults to None.
        """
        self.agent_name = agent_name
        self.instruction = instruction
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.max_iterations = max_iterations
        self.parallel_tool_calls = parallel_tool_calls
        self.streaming_timeout = streaming_timeout
        # Extra request params from config (provider-specific + FA params)
        self.request_params: Dict[str, Any] = request_params or {}
        # servers = list of MCP server names from fastagent.config.yaml
        self.servers: List[str] = list(_DEFAULT_SERVERS) if servers is None else servers
        self.tools_config = tools_config or {}
        # When False, <thinking>…</thinking> blocks are stripped before returning
        self.show_thinking = show_thinking
        # Optional API key / base URL (from pyclaw.yaml providers.minimax)
        self.api_key = api_key
        self.base_url = base_url
        # The agent name sent as X-Agent-Name header to the pyclaw MCP server.
        # Defaults to agent_name but session runners override to the base agent name.
        self.owner_name: str = owner_name or agent_name
        # Session ID — None for the base (non-session) runner; set for per-session runners.
        # Used as a prefix on all log lines so you can filter by session in agent.log.
        self.session_id: Optional[str] = session_id
        self._log_prefix: str = (
            f"[{self.owner_name}-{session_id.rsplit('-', 1)[-1]}]" if session_id else f"[{self.owner_name}]"
        )
        # Path to the history.json file for this session (None = no persistence)
        self.history_path: Optional[Path] = history_path
        self._history_loaded: bool = False
        self._app: Optional[Any] = None
        self._fa_context: Optional[Any] = None  # kept alive for the lifetime of the runner
        self._message_history: List[Dict[str, str]] = []
        # Serialises concurrent run()/run_stream() calls on the same session.
        # FastAgent does not support concurrent sends on the same app instance;
        # without this lock a second message while one is processing returns
        # immediately (empty result), causing the typing indicator to stop early.
        self._run_lock: asyncio.Lock = asyncio.Lock()
        # FA model-level settings applied at initialize() time
        self.reasoning_effort: Optional[str] = reasoning_effort
        self.text_verbosity: Optional[str] = text_verbosity
        self.service_tier: Optional[str] = service_tier
        # Full PyClaw config — used to build FastAgent Settings programmatically
        self.pyclaw_config: Optional[Any] = pyclaw_config
        # FastAgent app reference (set in initialize) — needed for ACP SlashCommandHandler
        self._fa_app: Optional[Any] = None
        # Cached ACP SlashCommandHandler (lazily created by acp_execute)
        self._slash_handler: Optional[Any] = None
        # ── Workflow state ────────────────────────────────────────────────────
        self.workflow: Optional[str] = workflow
        self.child_agent_configs: Dict[str, Any] = child_agent_configs or {}
        self.plan_type: str = plan_type
        self.plan_iterations: Optional[int] = plan_iterations
        self.generator: Optional[str] = generator
        self.evaluator: Optional[str] = evaluator
        self.min_rating: str = min_rating
        self.max_refinements: int = max_refinements
        self.refinement_instruction: Optional[str] = refinement_instruction
        self.worker: Optional[str] = worker
        self.k: int = k
        self.max_samples: int = max_samples
        self.match_strategy: str = match_strategy
        self.red_flag_max_length: Optional[int] = red_flag_max_length
        # Request priority — controls usage-based throttling.
        # "critical" = chat (never throttled), "normal" = jobs, "background" = vault/bulk ingest
        self.priority: str = priority

    async def _load_history(self) -> None:
        """Load history from disk into the FastAgent agent (once per lifetime).

        Reads the FA-native ``history.json`` file from ``self.history_path`` and
        injects the messages into the live agent's message history via
        ``load_message_history``.  Immediately purges any corrupted
        ``assistant(stop=toolUse)+user(empty)`` pairs injected by
        ``reconcile_interrupted_history``.  No-ops if history has already been
        loaded, if no ``history_path`` is set, or if the file does not exist.

        Returns:
            None
        """
        if self._history_loaded:
            return
        self._history_loaded = True
        # Workflow runners (orchestrator, evaluator_optimizer, etc.) manage their
        # own internal state — skip external history persistence for now.
        if self.workflow:
            return
        if self.history_path is None or not self.history_path.exists():
            return
        try:
            from fast_agent.mcp.prompt_serialization import load_messages
            messages = load_messages(str(self.history_path))
            if messages:
                agent = self._app._agent(None)
                agent.load_message_history(messages)
                # reconcile_interrupted_history (called inside load_message_history)
                # injects corrupted assistant(stop=toolUse)+user(empty) pairs.
                # Purge them immediately so they never reach the LLM.
                # message_history is a read-only property — mutate the list in-place.
                clean = _purge_corrupted_pairs(list(agent.message_history))
                agent.message_history[:] = clean
                logger.debug(
                    f"Loaded {len(messages)} history messages for {self.agent_name}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to load history for {self.agent_name}: {e}"
            )

    async def _save_history(self) -> None:
        """Save FastAgent's current message history to disk (atomic with rotation).

        Strips trailing FastAgent error responses and their preceding user turns,
        purges corrupted tool-call pairs, strips tool-call/result plumbing from
        the copy going to disk, then writes atomically to a temp file and rotates
        current → previous before promoting the new file.  The live in-memory
        ``agent.message_history`` is also updated so it stays consistent with
        what is saved.

        No-ops if this runner wraps a workflow agent, if ``history_path`` is None,
        or if the FastAgent app has not been initialized yet.

        Returns:
            None
        """
        if self.workflow:
            return
        if self.history_path is None or self._app is None:
            return
        try:
            from fast_agent.mcp.prompt_serialization import save_messages as _save
            agent = self._app._agent(None)
            # Strip trailing FastAgent error responses (and their preceding user turn) from
            # the in-memory history before saving so poisoned exchanges never reach disk.
            hist = agent.message_history
            while hist and _is_fastagent_error_msg(hist[-1]):
                hist.pop()
                if hist and getattr(hist[-1], "role", None) == "user":
                    hist.pop()
            hist = _purge_corrupted_pairs(hist)
            # Write the purged history back to the live agent so in-memory state
            # stays consistent with what's saved to disk.  Without this, corrupted
            # pairs remain in agent.message_history even after a clean save, and the
            # next _app.send() sends them to the LLM causing 2013 errors.
            # message_history is a read-only property — mutate the list in-place.
            agent.message_history[:] = hist
            # Strip tool-call/result plumbing from the copy going to disk.
            # The live agent.message_history is left intact for the active session.
            messages = _strip_tool_machinery(hist)
            messages = _trim_history_for_save(messages)
            if not messages:
                return
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            prev_path = self.history_path.parent / "history_previous.json"
            # Write to a temp file first (atomic)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=self.history_path.parent,
                prefix=".history.tmp.",
                suffix=".json",
            ) as tmp_fh:
                tmp_path = Path(tmp_fh.name)
            _save(messages, str(tmp_path))
            # Rotate current → previous, then promote temp → current
            if self.history_path.exists():
                os.replace(self.history_path, prev_path)
            os.replace(tmp_path, self.history_path)
            logger.debug(
                f"Saved {len(messages)} history messages for {self.agent_name}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to save history for {self.agent_name}: {e}"
            )

    def _all_servers(self) -> List[str]:
        """Return deduplicated list of servers needed by parent and all child agents.

        Collects MCP server names from ``self.servers`` and from every child agent
        config's ``servers`` list, preserving insertion order.

        Returns:
            List[str]: Ordered, deduplicated list of MCP server names.
        """
        seen: Dict[str, None] = dict.fromkeys(self.servers)
        for child_cfg in (getattr(self, "child_agent_configs", None) or {}).values():
            for s in (child_cfg.get("servers") or []):
                seen.setdefault(s, None)
        return list(seen)

    def _build_fa_settings(self) -> Any:
        """Build a FastAgent Settings object directly from pyclaw config.

        Eliminates the need for a ``fastagent.config.yaml`` file.  All MCP
        server connection details and provider credentials come from the live
        PyClaw config object.  Unknown server names (not in ``_BUILTIN_SERVERS``)
        are skipped with a warning.

        Returns:
            Any: A constructed ``fast_agent.config.Settings`` instance with MCP
            server definitions and provider credentials populated from pyclaw config.
        """
        from fast_agent.config import (
            Settings, MCPSettings, MCPServerSettings,
            LoggerSettings, AnthropicSettings, OpenAISettings, GoogleSettings, GenericSettings,
        )
        cfg = self.pyclaw_config
        mcp_port: int = cfg.gateway.mcp_port if cfg is not None else 8081

        # ── MCP servers ──────────────────────────────────────────────────────
        headers: Dict[str, str] = {"x-agent-name": self.owner_name}
        if self.session_id:
            headers["x-session-id"] = self.session_id

        servers: Dict[str, MCPServerSettings] = {}

        for name in (self._all_servers() or ["pyclaw"]):
            if name == "pyclaw":
                servers["pyclaw"] = MCPServerSettings(
                    url=f"http://localhost:{mcp_port}/mcp",
                    headers=headers,
                )
            elif name == "fetch":
                servers["fetch"] = MCPServerSettings(
                    command="uvx", args=["mcp-server-fetch"], load_on_start=False,
                )
            elif name == "time":
                tz = getattr(cfg, "timezone", None) if cfg else None
                if not tz:
                    try:
                        import datetime
                        tz_obj = datetime.datetime.now().astimezone().tzinfo
                        tz = getattr(tz_obj, "key", None)
                    except Exception:
                        pass
                args = ["mcp-server-time"]
                if tz:
                    args += ["--local-timezone", tz]
                servers["time"] = MCPServerSettings(command="uvx", args=args, load_on_start=False)
            elif name == "filesystem":
                servers["filesystem"] = MCPServerSettings(
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-filesystem",
                          str(Path.home()), "/tmp"],
                    load_on_start=False,
                )
            elif name == "chrome-devtools":
                cdp = getattr(getattr(cfg, "browser", None), "chrome_devtools_mcp", None) if cfg else None
                if cdp and cdp.enabled:
                    cdp_args: List[str] = []
                    if cdp.auto_connect:
                        cdp_args.append("--autoConnect")
                    if cdp.browser_url:
                        cdp_args += ["--browserUrl", cdp.browser_url]
                    if cdp.headless:
                        cdp_args.append("--headless")
                    if cdp.channel:
                        cdp_args += ["--channel", cdp.channel]
                    if cdp.executable_path:
                        cdp_args += ["--executablePath", cdp.executable_path]
                    if cdp.slim:
                        cdp_args.append("--slim")
                    servers["chrome-devtools"] = MCPServerSettings(
                        command=cdp.command, args=cdp_args, load_on_start=False,
                    )
                else:
                    logger.debug(
                        f"{self._log_prefix} chrome-devtools requested but not enabled in config"
                    )
            else:
                logger.warning(
                    f"{self._log_prefix} Unknown MCP server {name!r} — "
                    "define it under pyclaw config to use custom servers"
                )

        # ── Provider credentials ──────────────────────────────────────────────
        provider_kwargs: Dict[str, Any] = {}
        if cfg is not None:
            providers = getattr(cfg, "providers", None)
            if providers:
                p_anthropic = getattr(providers, "anthropic", None)
                if p_anthropic and getattr(p_anthropic, "api_key", None):
                    provider_kwargs["anthropic"] = AnthropicSettings(api_key=p_anthropic.api_key)

                p_openai = getattr(providers, "openai", None)
                if p_openai and getattr(p_openai, "api_key", None):
                    provider_kwargs["openai"] = OpenAISettings(api_key=p_openai.api_key)

                p_google = getattr(providers, "google", None)
                if p_google and getattr(p_google, "api_key", None):
                    provider_kwargs["google"] = GoogleSettings(api_key=p_google.api_key)

                # Generic provider — use per-runner api_key/base_url if available
                # (set by agent.py from the specific provider config for this model),
                # otherwise fall back to searching all providers for any generic one.
                if "generic." in self.model and (self.api_key or self.base_url):
                    provider_kwargs["generic"] = GenericSettings(
                        api_key=self.api_key,
                        base_url=self.base_url,
                    )
                elif "generic." in self.model:
                    _all_providers = {
                        name: getattr(providers, name, None)
                        for name in ("minimax", "fastagent", "openai", "anthropic", "google")
                    }
                    _all_providers.update(providers.model_extra or {})
                    for _p in _all_providers.values():
                        if _p and getattr(_p, "fastagent_provider", None) == "generic":
                            _base_url = getattr(_p, "api_url", None) or getattr(_p, "base_url", None)
                            provider_kwargs["generic"] = GenericSettings(
                                api_key=getattr(_p, "api_key", None),
                                base_url=_base_url,
                            )
                            break
        elif "generic." in self.model and (self.api_key or self.base_url):
            # No pyclaw_config — fall back to individual api_key/base_url fields
            provider_kwargs["generic"] = GenericSettings(
                api_key=self.api_key,
                base_url=self.base_url,
            )

        return Settings(
            default_model="passthrough",
            logger=LoggerSettings(
                progress_display=False,
                show_chat=False,
                show_tools=False,
                streaming="none",
                enable_markup=False,
            ),
            mcp=MCPSettings(servers=servers),
            **provider_kwargs,
        )

    def _register_workflow(self, fast: Any, parent_rp: Any, fa_settings: Any) -> None:
        """Register child agents and workflow decorator on fast before FA context starts.

        Called from ``initialize()`` when ``self.workflow`` is set.  All child agents
        are registered first (required by FA's decorator ordering rules) then the
        workflow agent is registered with ``default=True`` so ``_app.send()`` routes
        to it automatically.

        Args:
            fast (Any): The ``FastAgent`` application instance to decorate.
            parent_rp (Any): The ``FARequestParams`` for the parent/workflow agent.
            fa_settings (Any): The ``fast_agent.config.Settings`` used to resolve which
                MCP servers are available.

        Returns:
            None

        Raises:
            ValueError: If ``evaluator_optimizer`` workflow is missing ``generator`` or
                ``evaluator`` agent names, or if ``maker`` workflow is missing a ``worker``
                agent name, or if an unknown workflow type is specified.
        """
        from fast_agent.llm.request_params import RequestParams as FARequestParams

        available_servers = set(fa_settings.mcp.servers or {})

        # Register each child agent
        for child_name, child_cfg in self.child_agent_configs.items():
            child_servers = [s for s in (child_cfg.get("servers") or []) if s in available_servers]
            child_model = child_cfg.get("model") or self.model
            child_instruction = child_cfg.get("instruction") or f"You are {child_name}."
            child_rp = FARequestParams(maxTokens=child_cfg.get("max_tokens") or 16384)

            @fast.agent(
                name=child_name,
                instruction=child_instruction,
                model=child_model,
                servers=child_servers,
                request_params=child_rp,
            )
            async def _child():
                pass

        # Register the workflow decorator
        if self.workflow == "orchestrator":
            @fast.orchestrator(
                name=self.agent_name,
                instruction=self.instruction,
                model=self.model,
                agents=list(self.child_agent_configs),
                plan_type=self.plan_type,
                plan_iterations=self.plan_iterations if self.plan_iterations is not None else 5,
                request_params=parent_rp,
                default=True,
            )
            async def _workflow():
                pass

        elif self.workflow == "iterative_planner":
            @fast.iterative_planner(
                name=self.agent_name,
                instruction=self.instruction,
                model=self.model,
                agents=list(self.child_agent_configs),
                plan_iterations=self.plan_iterations if self.plan_iterations is not None else -1,
                request_params=parent_rp,
                default=True,
            )
            async def _workflow():
                pass

        elif self.workflow == "evaluator_optimizer":
            if not self.generator or not self.evaluator:
                raise ValueError(
                    f"evaluator_optimizer workflow requires 'generator' and 'evaluator' agent names"
                )
            @fast.evaluator_optimizer(
                name=self.agent_name,
                generator=self.generator,
                evaluator=self.evaluator,
                min_rating=self.min_rating,
                max_refinements=self.max_refinements,
                refinement_instruction=self.refinement_instruction,
                default=True,
            )
            async def _workflow():
                pass

        elif self.workflow == "maker":
            if not self.worker:
                raise ValueError("maker workflow requires a 'worker' agent name")
            @fast.maker(
                name=self.agent_name,
                worker=self.worker,
                k=self.k,
                max_samples=self.max_samples,
                match_strategy=self.match_strategy,
                red_flag_max_length=self.red_flag_max_length,
                default=True,
            )
            async def _workflow():
                pass

        else:
            raise ValueError(f"Unknown workflow type: {self.workflow!r}")

        logger.debug(
            f"{self._log_prefix} Registered workflow={self.workflow!r} "
            f"with children={list(self.child_agent_configs)}"
        )

    async def initialize(self) -> None:
        """Initialize the FastAgent app with configured MCP servers.

        Builds a ``FastAgent`` instance, injects the ``Settings`` object built from
        pyclaw config (replacing any on-disk ``fastagent.config.yaml``), registers
        the agent or workflow via decorators, enters the ``fast.run()`` context, and
        applies model-level settings (reasoning_effort, text_verbosity, service_tier).

        No-ops if ``self._app`` is already set (i.e. already initialized).

        Returns:
            None
        """
        if self._app is not None:
            return

        # Patch OpenAILLM for providers that use delta.reasoning_details (e.g. MiniMax)
        if "generic." in self.model:
            _patch_openai_llm_for_reasoning_details()

        from fast_agent import FastAgent
        from fast_agent.config import update_global_settings

        fast = FastAgent(self.agent_name, quiet=True, parse_cli_args=False)

        # Build and inject our Settings — replaces whatever FastAgent loaded from disk.
        # This is the single source of truth for MCP servers and provider credentials.
        fa_settings = self._build_fa_settings()
        fast.app._config_or_path = fa_settings
        update_global_settings(fa_settings)

        # Only activate servers we actually defined in settings (unknown names skipped above)
        active_servers = [s for s in self.servers if s in (fa_settings.mcp.servers or {})]

        from fast_agent.llm.request_params import RequestParams as FARequestParams

        # Fields known to FARequestParams — routed directly into rp_kwargs.
        # Anything else is forwarded as extra_body to the raw API call.
        _FA_PARAMS = {
            "temperature", "maxTokens", "max_tokens", "stopSequences",
            "use_history", "max_iterations", "parallel_tool_calls",
            "response_format", "streaming_timeout", "top_p", "top_k",
            "min_p", "presence_penalty", "frequency_penalty",
            "repetition_penalty", "service_tier",
        }

        # Start from individual fields (backwards compat)
        rp_kwargs: Dict[str, Any] = {"maxTokens": self.max_tokens or 16384}
        if self.top_p is not None:
            rp_kwargs["top_p"] = self.top_p
        if self.max_iterations is not None:
            rp_kwargs["max_iterations"] = self.max_iterations
        if self.parallel_tool_calls is not None:
            rp_kwargs["parallel_tool_calls"] = self.parallel_tool_calls
        if self.streaming_timeout is not None:
            rp_kwargs["streaming_timeout"] = self.streaming_timeout

        # Overlay with request_params from config; unknown keys → extra_body
        extra_body: Dict[str, Any] = {}
        for key, val in self.request_params.items():
            if key in _FA_PARAMS:
                rp_kwargs[key] = val
            else:
                extra_body[key] = val

        if extra_body:
            rp_kwargs["metadata"] = {"extra_body": extra_body}

        rp = FARequestParams(**rp_kwargs)

        if self.workflow and self.child_agent_configs:
            self._register_workflow(fast, rp, fa_settings)
        else:
            @fast.agent(
                name=self.agent_name,
                instruction=self.instruction,
                model=self.model,
                servers=active_servers,
                request_params=rp,
            )
            async def main():
                pass

        # Keep the FastAgent context alive for the lifetime of this runner.
        # Using explicit __aenter__ / __aexit__ instead of `async with` so the
        # context is NOT closed at the end of initialize() — it stays open until
        # cleanup() is called.
        self._fa_app = fast.app   # AgentApp — used for ACP SlashCommandHandler
        self._fa_context = fast.run()
        self._app = await self._fa_context.__aenter__()

        # Apply FA model-level settings configured at the agent level
        self._apply_fa_model_settings()

        logger.info(
            f"Initialized agent runner: {self.agent_name} "
            f"(model={self.model}, servers={active_servers})"
        )
    
    def _apply_fa_model_settings(self) -> None:
        """Apply reasoning_effort, text_verbosity, and service_tier to the FA agent.

        Called once at the end of ``initialize()``.  Each setting is optional — if not
        configured it is left at FastAgent's default.  Failures are logged and
        swallowed so a misconfigured setting never prevents the runner from starting.

        Returns:
            None
        """
        if not any([self.reasoning_effort, self.text_verbosity, self.service_tier]):
            return
        try:
            agent = self._app._agent(None)
            llm = getattr(agent, "llm", None)
            if llm is None:
                logger.debug(f"{self._log_prefix} FA agent has no .llm — skipping model settings")
                return

            if self.reasoning_effort is not None:
                from fast_agent.llm.reasoning_effort import parse_reasoning_setting
                setting = parse_reasoning_setting(self.reasoning_effort)
                llm.set_reasoning_effort(setting)
                logger.debug(f"{self._log_prefix} reasoning_effort={self.reasoning_effort!r}")

            if self.text_verbosity is not None:
                llm.set_text_verbosity(self.text_verbosity)
                logger.debug(f"{self._log_prefix} text_verbosity={self.text_verbosity!r}")

            if self.service_tier is not None:
                llm.set_service_tier(self.service_tier)
                logger.debug(f"{self._log_prefix} service_tier={self.service_tier!r}")

        except Exception as e:
            logger.warning(f"{self._log_prefix} Could not apply FA model settings: {e}")

    async def run(self, prompt: str) -> str:
        """Run a single prompt through the agent and return the complete response.

        Acquires the run lock (serialising concurrent calls on the same runner),
        loads history on first call, enforces per-model concurrency limits, and
        saves history after a successful turn.  Strips ``<thinking>`` blocks
        unless ``show_thinking`` is True.

        Args:
            prompt (str): User prompt to send to the agent.

        Returns:
            str: The agent's text response with thinking blocks stripped (unless
            ``show_thinking`` is True).

        Raises:
            RuntimeError: If FastAgent returns an internal-error response string.
        """
        if self._app is None:
            await self.initialize()

        _agent_logger = logging.getLogger(f"pyclaw.agent.{self.owner_name}")
        _p = self._log_prefix

        async with self._run_lock:
            await self._load_history()

            self._message_history.append({"role": "user", "content": prompt})

            _completed = False
            _agent_logger.info("%s [TURN] prompt: %s", _p, prompt[:500] + ("…" if len(prompt) > 500 else ""))
            try:
                # Enforce per-model concurrency limit (usage-throttle check included)
                from pyclaw.core.concurrency import get_manager
                async with get_manager().acquire(self.model, self.priority):
                    result = await self._app.send(prompt)
                response = str(result)

                # FastAgent catches model errors and returns them as a plain string instead
                # of raising — detect and raise so the error is not saved to history.
                if response.startswith(_FA_ERROR_PREFIX):
                    raise RuntimeError(response)

                # Strip <thinking> blocks unless explicitly shown
                if not self.show_thinking:
                    response = strip_thinking_tags(response)

                self._message_history.append({"role": "assistant", "content": response})
                _completed = True
                _agent_logger.info("%s [TURN] response: %s", _p, response[:500] + ("…" if len(response) > 500 else ""))
                return response
            finally:
                if _completed:
                    await self._save_history()
    
    async def run_stream(self, prompt: str) -> AsyncIterator[tuple[str, bool]]:
        """Run a prompt and stream the response as incremental chunks.

        Acquires the run lock (serialising concurrent calls), loads history on
        first call, enforces per-model concurrency limits, and saves history after
        a successful stream completes.

        Args:
            prompt (str): User prompt to send to the agent.

        Yields:
            tuple[str, bool]: ``(text_chunk, is_reasoning)`` pairs.
            ``is_reasoning=True`` for thinking/reasoning content,
            ``is_reasoning=False`` for normal response content.
        """
        if self._app is None:
            await self.initialize()

        _agent_logger = logging.getLogger(f"pyclaw.agent.{self.owner_name}")
        _p = self._log_prefix

        async with self._run_lock:
            await self._load_history()

            self._message_history.append({"role": "user", "content": prompt})

            _completed = False
            _agent_logger.info("%s [STREAM] prompt: %s", _p, prompt[:500] + ("…" if len(prompt) > 500 else ""))
            try:
                # Enforce per-model concurrency limit (usage-throttle check included)
                from pyclaw.core.concurrency import get_manager
                async with get_manager().acquire(self.model, self.priority):
                    async for item in self._run_stream_inner(prompt):
                        yield item
                _completed = True
            finally:
                if _completed:
                    _agent_logger.info("%s [STREAM] completed", _p)
                    await self._save_history()

    async def _run_stream_inner(self, prompt: str) -> AsyncIterator[tuple[str, bool]]:
        """Inner streaming implementation called under the concurrency lock.

        Registers a stream listener on the FA agent if the agent supports
        ``add_stream_listener``; otherwise falls back to a non-streaming send that
        yields the full response as a single chunk.

        Args:
            prompt (str): User prompt to send to the agent.

        Yields:
            tuple[str, bool]: ``(text_chunk, is_reasoning)`` pairs.

        Raises:
            RuntimeError: If the agent returns a FastAgent internal-error string.
        """
        # Get the agent and set up streaming
        agent = self._app._agent(None)

        # Check if agent supports streaming
        if hasattr(agent, 'add_stream_listener'):
            import asyncio
            from collections import deque

            # Queue holds (text, is_reasoning) tuples
            chunk_queue: deque[tuple[str, bool]] = deque()
            send_done = False

            def on_chunk(chunk):
                text = chunk.text if hasattr(chunk, 'text') else str(chunk)
                is_reasoning = bool(getattr(chunk, 'is_reasoning', False))
                if text:
                    chunk_queue.append((text, is_reasoning))

            remove_listener = agent.add_stream_listener(on_chunk)

            try:
                send_task = asyncio.create_task(agent.send(prompt))

                while not send_done or chunk_queue:
                    while chunk_queue:
                        yield chunk_queue.popleft()
                    if send_task.done():
                        send_done = True
                    else:
                        await asyncio.sleep(0.01)

                result = await send_task
                # Raise so run_stream() skips _save_history and the caller
                # (gateway) evicts the corrupted session runner.
                if isinstance(result, str) and result.startswith(_FA_ERROR_PREFIX):
                    raise RuntimeError(result)

            finally:
                remove_listener()
        else:
            # Fall back to non-streaming — yield full response as one response chunk
            result = await self._app.send(prompt)
            yield (str(result), False)
    
    async def inject_turns(self, turns: list) -> None:
        """Inject synthetic PromptMessageExtended turns without an LLM call.

        Turns is a list of dicts matching the PromptMessageExtended JSON schema
        (role, content, tool_calls/tool_results, stop_reason).  The turns are
        deserialised through FastAgent's own prompt_serialization pipeline and
        appended to the live message history, then saved to disk.

        Args:
            turns (list): List of dicts matching the ``PromptMessageExtended`` JSON
                schema with keys such as ``role``, ``content``, ``stop_reason``.

        Returns:
            None
        """
        if self._app is None:
            await self.initialize()

        async with self._run_lock:
            await self._load_history()

            import json
            import tempfile

            tmp_path = None
            try:
                from fast_agent.mcp.prompt_serialization import load_messages
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                ) as tmp:
                    json.dump({"messages": turns}, tmp)
                    tmp_path = tmp.name
                messages = load_messages(tmp_path)
            except Exception as e:
                logger.warning(f"{self._log_prefix} inject_turns: could not build messages: {e}")
                return
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            if not messages:
                return

            agent = self._app._agent(None)
            try:
                agent.append_history(messages)
            except AttributeError:
                agent.message_history.extend(messages)

            await self._save_history()
            logger.info(
                f"{self._log_prefix} inject_turns: appended {len(messages)} synthetic turns"
            )

    async def acp_execute(self, command_name: str, arguments: str) -> str:
        """Execute a FastAgent ACP slash command and return a string response.

        Uses FastAgent's ``SlashCommandHandler`` to run built-in ACP commands
        such as ``model reasoning``, ``history``, ``clear``, etc.  The handler
        is lazily created on first call and cached for the lifetime of this runner.

        Args:
            command_name (str): ACP slash command name without the leading ``/``
                (e.g. ``"model"``, ``"history"``).
            arguments (str): Arguments string passed verbatim to the command handler.

        Returns:
            str: The command's text response, or an error message if unavailable.
        """
        if self._app is None:
            return "Agent not initialized."
        try:
            from fast_agent.acp.slash_commands import SlashCommandHandler
            from fast_agent.core.fastagent import AgentInstance

            if self._slash_handler is None:
                agent_obj = self._app._agent(None)
                instance = AgentInstance(
                    app=self._fa_app or self._app,
                    agents={self.agent_name: agent_obj},
                )
                self._slash_handler = SlashCommandHandler(
                    session_id=self.session_id or f"pyclaw-{self.agent_name}",
                    instance=instance,
                    primary_agent_name=self.agent_name,
                )
            return await self._slash_handler.execute_command(command_name, arguments)
        except Exception as e:
            logger.warning(f"ACP execute failed for /{command_name} {arguments!r}: {e}")
            return f"/{command_name} unavailable: {e}"

    async def cleanup(self) -> None:
        """Close the FastAgent context and release MCP connections.

        Calls ``__aexit__`` on the FastAgent ``fast.run()`` context, which closes
        all MCP server connections.  Safe to call multiple times.

        Returns:
            None
        """
        if self._fa_context is not None:
            try:
                await self._fa_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing FastAgent context: {e}")
            self._fa_context = None
            self._app = None

    def get_history(self) -> List[Dict[str, str]]:
        """Return a copy of the in-memory message history for this runner.

        Returns:
            List[Dict[str, str]]: A copy of the list of message dicts, each
            containing ``role`` and ``content`` keys.
        """
        return self._message_history.copy()




