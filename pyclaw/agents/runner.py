"""Agent runner using FastAgent."""
import asyncio
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
    """Remove <thinking>...</thinking> blocks from *text* and normalise whitespace."""
    stripped = _THINKING_RE.sub("", text)
    # Collapse more than two consecutive newlines left behind by removals
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def format_thinking_for_telegram(text: str) -> Optional[str]:
    """Return a single HTML-formatted Telegram message with thinking shown
    as an inline spoiler followed by the response.

    The thinking block is hidden behind a tap-to-reveal spoiler; the response
    follows immediately after so everything arrives in one message.

    Returns None if the text contains no thinking blocks.
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

# Default MCP servers every agent gets (can be extended per-agent).
# "pyclaw" provides all built-in tools: bash, memory, todos, sessions, etc.
_DEFAULT_SERVERS: List[str] = ["pyclaw"]

# All available server names (defined in fastagent.config.yaml)
ALL_SERVERS = ["pyclaw", "fetch", "time", "filesystem"]


class AgentRunner:
    """
    Runner for FastAgent-based execution.

    Wires MCP servers from agent config so tools are available to the agent.
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
        streaming_timeout: Optional[float] = None,
        servers: Optional[List[str]] = None,
        tools_config: Optional[Dict[str, Any]] = None,
        show_thinking: bool = False,
        api_key: Optional[str] = None,
        owner_name: Optional[str] = None,
        request_params: Optional[Dict[str, Any]] = None,
        history_path: Optional[Path] = None,
    ):
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
        self.servers: List[str] = servers or list(_DEFAULT_SERVERS)
        self.tools_config = tools_config or {}
        # When False, <thinking>…</thinking> blocks are stripped before returning
        self.show_thinking = show_thinking
        # Optional API key (e.g. from pyclaw.yaml providers.minimax.api_key)
        self.api_key = api_key
        # The agent name sent as X-Agent-Name header to the pyclaw MCP server.
        # Defaults to agent_name but session runners override to the base agent name.
        self.owner_name: str = owner_name or agent_name
        # Path to the history.json file for this session (None = no persistence)
        self.history_path: Optional[Path] = history_path
        self._history_loaded: bool = False
        self._app: Optional[Any] = None
        self._fa_context: Optional[Any] = None  # kept alive for the lifetime of the runner
        self._message_history: List[Dict[str, str]] = []

    async def _load_history(self) -> None:
        """Load history from disk into the FastAgent agent (once per lifetime)."""
        if self._history_loaded:
            return
        self._history_loaded = True
        if self.history_path is None or not self.history_path.exists():
            return
        try:
            from fast_agent.mcp.prompt_serialization import load_messages
            messages = load_messages(str(self.history_path))
            if messages:
                agent = self._app._agent(None)
                agent.load_message_history(messages)
                logger.debug(
                    f"Loaded {len(messages)} history messages for {self.agent_name}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to load history for {self.agent_name}: {e}"
            )

    async def _save_history(self) -> None:
        """Save FastAgent's current message history to disk (atomic with rotation)."""
        if self.history_path is None or self._app is None:
            return
        try:
            from fast_agent.mcp.prompt_serialization import save_messages as _save
            agent = self._app._agent(None)
            messages = agent.message_history
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

    async def initialize(self):
        """Initialize the FastAgent app with configured MCP servers."""
        if self._app is not None:
            return

        # Set up MiniMax generic provider if needed
        if "generic." in self.model:
            # Priority: explicit api_key arg → MINIMAX_API_KEY env → keychain
            api_key = self.api_key or os.environ.get("MINIMAX_API_KEY")
            if not api_key:
                try:
                    import subprocess
                    api_key = subprocess.check_output(
                        ["security", "find-generic-password", "-s", "pyclaw",
                         "-a", "minimax-api-key", "-w"],
                        text=True,
                    ).strip()
                except Exception:
                    pass
            if api_key:
                os.environ["GENERIC_API_KEY"] = api_key
            os.environ["GENERIC_BASE_URL"] = "https://api.minimax.io/v1"

            # Ensure OpenAILLM handles delta.reasoning_details (MiniMax extension)
            _patch_openai_llm_for_reasoning_details()

        # Ensure fastagent.config.yaml is findable from CWD
        _ensure_fastagent_config()

        from fast_agent import FastAgent

        fast = FastAgent(self.agent_name)

        # Inject X-Agent-Name header into the pyclaw HTTP MCP server config
        # so the server can identify which agent is calling.
        try:
            pyclaw_cfg = fast.config.get("mcp", {}).get("servers", {}).get("pyclaw")
            if pyclaw_cfg is not None and isinstance(pyclaw_cfg, dict):
                existing = pyclaw_cfg.get("headers") or {}
                pyclaw_cfg["headers"] = {**existing, "x-agent-name": self.owner_name}
        except Exception as e:
            logger.debug(f"Could not set X-Agent-Name header on pyclaw MCP server: {e}")

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

        @fast.agent(
            instruction=self.instruction,
            model=self.model,
            servers=self.servers,
            request_params=rp,
        )
        async def main():
            pass

        # Keep the FastAgent context alive for the lifetime of this runner.
        # Using explicit __aenter__ / __aexit__ instead of `async with` so the
        # context is NOT closed at the end of initialize() — it stays open until
        # cleanup() is called.
        self._fa_context = fast.run()
        self._app = await self._fa_context.__aenter__()
        logger.info(
            f"Initialized agent runner: {self.agent_name} "
            f"(model={self.model}, servers={self.servers})"
        )
    
    async def run(self, prompt: str) -> str:
        """Run a single prompt through the agent.

        Args:
            prompt: User prompt

        Returns:
            Agent response content
        """
        if self._app is None:
            await self.initialize()

        await self._load_history()

        self._message_history.append({"role": "user", "content": prompt})

        _completed = False
        try:
            # Enforce per-model concurrency limit
            from pyclaw.core.concurrency import get_manager
            async with get_manager().acquire(self.model):
                result = await self._app.send(prompt)
            response = str(result)

            # Strip <thinking> blocks unless explicitly shown
            if not self.show_thinking:
                response = strip_thinking_tags(response)

            self._message_history.append({"role": "assistant", "content": response})
            _completed = True
            return response
        finally:
            if _completed:
                await self._save_history()
    
    async def run_stream(self, prompt: str) -> AsyncIterator[tuple[str, bool]]:
        """Run a prompt and stream the response.

        Yields:
            (text_chunk, is_reasoning) tuples.  is_reasoning=True for thinking/
            reasoning content, False for normal response content.
        """
        if self._app is None:
            await self.initialize()

        await self._load_history()

        self._message_history.append({"role": "user", "content": prompt})

        _completed = False
        try:
            # Enforce per-model concurrency limit (same as run())
            from pyclaw.core.concurrency import get_manager
            async with get_manager().acquire(self.model):
                async for item in self._run_stream_inner(prompt):
                    yield item
            _completed = True
        finally:
            if _completed:
                await self._save_history()

    async def _run_stream_inner(self, prompt: str) -> AsyncIterator[tuple[str, bool]]:
        """Inner streaming implementation — called under the concurrency lock."""
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

                await send_task

            finally:
                remove_listener()
        else:
            # Fall back to non-streaming — yield full response as one response chunk
            result = await self._app.send(prompt)
            yield (str(result), False)
    
    async def cleanup(self) -> None:
        """Close the FastAgent context and release MCP connections."""
        if self._fa_context is not None:
            try:
                await self._fa_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing FastAgent context: {e}")
            self._fa_context = None
            self._app = None

    def get_history(self) -> List[Dict[str, str]]:
        """Get message history."""
        return self._message_history.copy()


def _ensure_fastagent_config() -> None:
    """
    Make sure a fastagent.config.yaml is findable from the current directory.
    FastAgent looks in CWD, then walks up. We also check ~/.pyclaw/.
    """
    cwd = Path.cwd()
    # Already present in cwd or a parent?
    for p in [cwd, *cwd.parents]:
        if (p / "fastagent.config.yaml").exists():
            return

    # Try ~/.pyclaw/fastagent.config.yaml — symlink into CWD if found
    user_cfg = Path("~/.pyclaw/fastagent.config.yaml").expanduser()
    if user_cfg.exists():
        target = cwd / "fastagent.config.yaml"
        try:
            target.symlink_to(user_cfg)
            logger.debug(f"Symlinked fastagent.config.yaml → {user_cfg}")
        except (FileExistsError, OSError):
            pass
        return

    # Fall back: check the project source tree
    src_cfg = Path(__file__).parent.parent.parent / "fastagent.config.yaml"
    if src_cfg.exists():
        target = cwd / "fastagent.config.yaml"
        try:
            target.symlink_to(src_cfg.resolve())
        except (FileExistsError, OSError):
            pass


