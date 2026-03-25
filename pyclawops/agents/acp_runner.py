"""ACP (Agent Client Protocol) runners for pyclawops.

Provides three runner classes:

  AcpRunner        — Client-side stdio ACP runner.  Spawns any ACP-compliant
                     coding agent (fast-agent-acp, claude-agent-acp, …) and
                     drives it from pyclawops with full streaming support.

  ClaudeCodeRunner — Non-interactive Claude Code wrapper.  Uses
                     ``claude -p`` with stream-json output for live streaming.

  OpenCodeRunner   — Non-interactive OpenCode wrapper.  Uses ``opencode run``
                     and yields the response as a single chunk.

Design notes
------------
* ``AcpRunner.run_stream()`` yields ``(text, is_reasoning)`` tuples — the same
  contract as ``AgentRunner.run_stream()`` so callers are interchangeable.
* ``ClaudeCodeRunner`` applies delta-slicing on cumulative content blocks
  (Claude Code's stream-json emits the full accumulated text each event).
* ``AcpRunner`` keeps the agent subprocess alive across multiple ``run()``
  calls — initialise once, reuse the session.
* ``PyclawAcpClient`` implements the full ``acp.interfaces.Client`` protocol,
  including file delegation and terminal management.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("pyclawops.agents.acp_runner")


# ---------------------------------------------------------------------------
# ACP Client implementation (used by AcpRunner)
# ---------------------------------------------------------------------------

class PyclawAcpClient:
    """
    Implements the ``acp.interfaces.Client`` protocol for pyclawops.

    Handles:
    - Streaming ``AgentMessageChunk`` / ``AgentThoughtChunk`` via
      ``chunk_callback(text, is_reasoning)``.
    - Tool permission requests via ``permission_handler`` (default: allow_once).
    - File read/write delegation to the local filesystem.
    - Terminal management via asyncio subprocesses.
    """

    def __init__(
        self,
        permission_handler: Optional[Callable] = None,
        chunk_callback: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        """Initialize the ACP client with optional permission and chunk callbacks.

        Args:
            permission_handler (Optional[Callable]): Async callable with signature
                ``(options, tool_call) -> PermissionOptionKind str`` invoked when the
                agent requests a tool execution permission. Defaults to auto-approving
                with ``"allow_once"`` when None.
            chunk_callback (Optional[Callable[[str, bool], None]]): Sync callable
                invoked for each streaming chunk with ``(text, is_reasoning)`` args.
                Defaults to None (chunks are discarded).
        """
        # async callable(options, tool_call) -> PermissionOptionKind str
        self._permission_handler = permission_handler
        # sync callable(text: str, is_reasoning: bool)
        self._chunk_callback = chunk_callback
        self._agent_conn: Optional[Any] = None
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}

    def on_connect(self, conn: Any) -> None:
        """Store the ACP agent connection object when the protocol connects.

        Args:
            conn (Any): The ACP agent connection object provided by the ACP library.

        Returns:
            None
        """
        self._agent_conn = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Handle an incremental session update from the ACP agent.

        Dispatches ``AgentMessageChunk`` and ``AgentThoughtChunk`` updates to the
        registered ``chunk_callback`` with ``is_reasoning=False`` and
        ``is_reasoning=True`` respectively.

        Args:
            session_id (str): The ACP session identifier.
            update (Any): An ACP schema update object, typically
                ``AgentMessageChunk`` or ``AgentThoughtChunk``.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            None
        """
        from acp.schema import AgentMessageChunk, AgentThoughtChunk

        cb = self._chunk_callback
        if cb is None:
            return
        if isinstance(update, AgentMessageChunk):
            content = update.content
            if hasattr(content, "text") and content.text:
                cb(content.text, False)
        elif isinstance(update, AgentThoughtChunk):
            content = update.content
            if hasattr(content, "text") and content.text:
                cb(content.text, True)

    async def request_permission(
        self, options: list, session_id: str, tool_call: Any, **kwargs: Any
    ) -> Any:
        """Handle a tool permission request from the ACP agent.

        Invokes ``permission_handler`` if set; otherwise defaults to
        ``"allow_once"``.  Returns a ``RequestPermissionResponse`` with either an
        ``AllowedOutcome`` (selected option) or a ``DeniedOutcome`` (cancelled).

        Args:
            options (list): List of ``PermissionOption`` objects the agent is
                requesting approval for.
            session_id (str): The ACP session identifier.
            tool_call (Any): The tool call object requesting permission.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``RequestPermissionResponse`` schema object.
        """
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        if self._permission_handler:
            chosen_kind = await self._permission_handler(options, tool_call)
        else:
            chosen_kind = "allow_once"

        chosen = next(
            (o for o in options if o.kind == chosen_kind),
            options[0] if options else None,
        )
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id)
        )

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> Any:
        """Write text content to a local file on behalf of the ACP agent.

        Args:
            content (str): Text content to write.
            path (str): Absolute or relative path of the file to write.
            session_id (str): The ACP session identifier.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``WriteTextFileResponse`` schema object.
        """
        from acp.schema import WriteTextFileResponse

        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning(f"ACP write_text_file {path}: {exc}")
        return WriteTextFileResponse()

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: Optional[int] = None,
        line: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Read text content from a local file on behalf of the ACP agent.

        Supports optional line-offset and character/line limiting.

        Args:
            path (str): Absolute or relative path of the file to read.
            session_id (str): The ACP session identifier.
            limit (Optional[int]): Maximum number of lines (when ``line`` is set) or
                characters (without ``line``) to return. Defaults to None (no limit).
            line (Optional[int]): 1-based starting line number. When set, ``limit``
                is treated as a line count. Defaults to None.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``ReadTextFileResponse`` schema object with ``content`` field.
        """
        from acp.schema import ReadTextFileResponse

        try:
            text = Path(path).read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            if line is not None:
                start = max(0, line - 1)
                end = start + (limit or len(lines))
                text = "".join(lines[start:end])
            elif limit is not None:
                text = text[:limit]
        except Exception as exc:
            logger.warning(f"ACP read_text_file {path}: {exc}")
            text = ""
        return ReadTextFileResponse(content=text)

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: Optional[list] = None,
        cwd: Optional[str] = None,
        env: Optional[list] = None,
        output_byte_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Spawn a terminal subprocess on behalf of the ACP agent.

        Creates an asyncio subprocess and tracks it under a UUID terminal ID.

        Args:
            command (str): Executable to run.
            session_id (str): The ACP session identifier.
            args (Optional[list]): Additional CLI arguments. Defaults to None.
            cwd (Optional[str]): Working directory for the subprocess. Defaults to None.
            env (Optional[list]): List of environment variable objects with ``name``
                and ``value`` attributes, merged over ``os.environ``. Defaults to None.
            output_byte_limit (Optional[int]): Not currently enforced. Defaults to None.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``CreateTerminalResponse`` schema object with ``terminal_id``.
        """
        from acp.schema import CreateTerminalResponse

        tid = str(uuid.uuid4())
        try:
            env_dict = {e.name: e.value for e in (env or [])}
            merged_env = {**os.environ, **env_dict} if env_dict else None
            proc = await asyncio.create_subprocess_exec(
                command,
                *(args or []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=merged_env,
            )
            self._terminals[tid] = proc
            logger.debug(f"ACP terminal {tid}: {command} {args}")
        except Exception as exc:
            logger.warning(f"ACP create_terminal failed: {exc}")
        return CreateTerminalResponse(terminal_id=tid)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        """Read buffered stdout output from a tracked terminal subprocess.

        Reads up to 64 KiB with a 5-second timeout.  Returns empty string on
        timeout or if the terminal is not found.

        Args:
            session_id (str): The ACP session identifier.
            terminal_id (str): UUID of the terminal returned by ``create_terminal``.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``TerminalOutputResponse`` schema object with ``output`` and
            ``truncated`` fields.
        """
        from acp.schema import TerminalOutputResponse

        proc = self._terminals.get(terminal_id)
        output = ""
        if proc and proc.stdout:
            try:
                raw = await asyncio.wait_for(proc.stdout.read(65536), timeout=5.0)
                output = raw.decode(errors="replace")
            except (asyncio.TimeoutError, Exception):
                pass
        return TerminalOutputResponse(output=output, truncated=False)

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        """Remove a terminal from the tracking registry without killing it.

        Args:
            session_id (str): The ACP session identifier.
            terminal_id (str): UUID of the terminal to release.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``ReleaseTerminalResponse`` schema object.
        """
        from acp.schema import ReleaseTerminalResponse

        self._terminals.pop(terminal_id, None)
        return ReleaseTerminalResponse()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        """Block until a tracked terminal subprocess exits and return its exit code.

        Args:
            session_id (str): The ACP session identifier.
            terminal_id (str): UUID of the terminal to wait on.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``WaitForTerminalExitResponse`` schema object with ``exit_code``.
        """
        from acp.schema import WaitForTerminalExitResponse

        proc = self._terminals.get(terminal_id)
        code = 0
        if proc:
            try:
                code = await proc.wait()
            except Exception:
                pass
        return WaitForTerminalExitResponse(exit_code=code)

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        """Send SIGTERM to a tracked terminal subprocess and remove it from registry.

        Args:
            session_id (str): The ACP session identifier.
            terminal_id (str): UUID of the terminal to kill.
            **kwargs (Any): Additional keyword arguments from the ACP protocol.

        Returns:
            Any: A ``KillTerminalCommandResponse`` schema object.
        """
        from acp.schema import KillTerminalCommandResponse

        proc = self._terminals.pop(terminal_id, None)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        return KillTerminalCommandResponse()

    async def ext_method(self, method: str, params: dict) -> dict:
        """Handle an extended ACP method call (no-op stub).

        Called by the ACP library for protocol extensions not covered by the
        standard interface.  Logs the method name and returns an empty dict.

        Args:
            method (str): Extended method name.
            params (dict): Method parameters.

        Returns:
            dict: Empty dictionary (extension not implemented).
        """
        logger.debug(f"ACP ext_method: {method}")
        return {}

    async def ext_notification(self, method: str, params: dict) -> None:
        """Handle an extended ACP notification (no-op stub).

        Called by the ACP library for protocol extension notifications.
        Logs the method name and takes no action.

        Args:
            method (str): Extended notification method name.
            params (dict): Notification parameters.

        Returns:
            None
        """
        logger.debug(f"ACP ext_notification: {method}")


# ---------------------------------------------------------------------------
# AcpRunner — Client-side stdio ACP runner
# ---------------------------------------------------------------------------

class AcpRunner:
    """
    Client-side stdio ACP runner.

    Spawns an ACP-compliant agent as a subprocess and communicates with it
    over NDJSON stdio.  Keeps the subprocess alive across multiple calls so
    session context is preserved.

    Compatible agents (must be on PATH or pass full path as ``command``):
      - ``fast-agent-acp``           (bundled with fast-agent)
      - ``claude-agent-acp``         (npm: @zed-industries/claude-agent-acp)
      - any other stdio ACP server

    Args:
        command:            Binary to spawn.
        args:               Extra CLI arguments.
        cwd:                Working directory for the subprocess and session.
        env:                Extra environment variables (merged over os.environ).
        model:              If set, call ``set_session_model`` after init.
        permission_handler: ``async (options, tool_call) -> PermissionOptionKind``.
                            Defaults to auto-approving with ``"allow_once"``.
    """

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        permission_handler: Optional[Callable] = None,
    ) -> None:
        """Initialize the ACP runner without starting the subprocess.

        The subprocess is not started until ``initialize()`` or the first
        ``run()``/``run_stream()`` call.

        Args:
            command (str): Binary to spawn as the ACP stdio server.
            args (Optional[List[str]]): Extra CLI arguments for the binary.
                Defaults to None.
            cwd (Optional[str]): Working directory for the subprocess and ACP session.
                Defaults to the current working directory.
            env (Optional[Dict[str, str]]): Extra environment variables merged over
                ``os.environ``. Defaults to None.
            model (Optional[str]): If set, ``set_session_model`` is called after
                session creation. Defaults to None.
            permission_handler (Optional[Callable]): Async callable
                ``(options, tool_call) -> PermissionOptionKind`` for tool permission
                decisions. Defaults to None (auto-approve with ``"allow_once"``).
        """
        self.command = command
        self.args = args or []
        self.cwd = cwd or os.getcwd()
        self.env = env or {}
        self.model = model
        self._permission_handler = permission_handler
        self._client: Optional[PyclawAcpClient] = None
        self._conn: Optional[Any] = None
        self._session_id: Optional[str] = None
        self._exit_stack: Optional[AsyncExitStack] = None

    async def initialize(self) -> None:
        """Spawn the agent subprocess and create an ACP session.

        Calls ``spawn_agent_process`` to start the subprocess, performs the ACP
        protocol handshake, creates a new session via ``conn.new_session``, and
        optionally sets the model with ``conn.set_session_model``.  No-ops if
        already initialized (``self._conn`` is not None).

        Returns:
            None
        """
        if self._conn is not None:
            return

        from acp import spawn_agent_process
        from acp.schema import ClientCapabilities, Implementation

        self._client = PyclawAcpClient(permission_handler=self._permission_handler)
        self._exit_stack = AsyncExitStack()

        full_env = {**os.environ, **self.env}
        conn, _proc = await self._exit_stack.enter_async_context(
            spawn_agent_process(
                self._client,
                self.command,
                *self.args,
                env=full_env,
                cwd=self.cwd,
            )
        )
        self._conn = conn

        await conn.initialize(
            protocol_version=1,
            client_info=Implementation(name="pyclawops", version="1.0"),
            client_capabilities=ClientCapabilities(),
        )

        session = await conn.new_session(cwd=self.cwd)
        self._session_id = session.session_id
        logger.info(
            f"AcpRunner: session {self._session_id} started "
            f"[{self.command} {' '.join(self.args)}]"
        )

        if self.model:
            try:
                await conn.set_session_model(
                    model_id=self.model, session_id=self._session_id
                )
                logger.debug(f"AcpRunner: model set to {self.model}")
            except Exception as exc:
                logger.warning(f"AcpRunner: set_session_model failed: {exc}")

    async def run(self, prompt: str) -> str:
        """Run a prompt through the ACP agent and return the full response text.

        Collects all non-reasoning chunks from ``run_stream`` and concatenates them.

        Args:
            prompt (str): User prompt to send to the agent.

        Returns:
            str: The agent's complete response text (reasoning chunks excluded).
        """
        chunks: List[str] = []
        async for text, is_reasoning in self.run_stream(prompt):
            if not is_reasoning:
                chunks.append(text)
        return "".join(chunks)

    async def run_stream(
        self, prompt: str
    ) -> AsyncIterator[Tuple[str, bool]]:
        """Run a prompt and stream incremental (text, is_reasoning) tuples.

        Calls ``initialize()`` if not yet started, then sends the prompt via
        ``conn.prompt()`` while routing streaming chunks from the agent's
        ``session_update`` callback through an asyncio Queue.

        Reasoning/thought chunks have ``is_reasoning=True``; response chunks
        have ``is_reasoning=False``.

        Args:
            prompt (str): User prompt to send to the agent.

        Yields:
            Tuple[str, bool]: ``(text_chunk, is_reasoning)`` pairs streamed as
            the agent produces output.
        """
        if self._conn is None:
            await self.initialize()

        from acp.schema import TextContentBlock

        chunk_queue: asyncio.Queue[Tuple[str, bool]] = asyncio.Queue()
        done = asyncio.Event()

        def on_chunk(text: str, is_reasoning: bool) -> None:
            chunk_queue.put_nowait((text, is_reasoning))

        assert self._client is not None
        self._client._chunk_callback = on_chunk

        async def _send() -> None:
            try:
                await self._conn.prompt(  # type: ignore[union-attr]
                    prompt=[TextContentBlock(type="text", text=prompt)],
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.error(f"AcpRunner prompt error: {exc}")
            finally:
                done.set()

        send_task = asyncio.create_task(_send())

        try:
            while True:
                try:
                    item = await asyncio.wait_for(chunk_queue.get(), timeout=0.1)
                    yield item
                except asyncio.TimeoutError:
                    if done.is_set() and chunk_queue.empty():
                        break
        finally:
            self._client._chunk_callback = None
            if not send_task.done():
                send_task.cancel()
                try:
                    await send_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def cleanup(self) -> None:
        """Shut down the agent subprocess and close the ACP connection.

        Calls ``aclose()`` on the ``AsyncExitStack`` that owns the subprocess
        context, then clears all internal state.  Safe to call multiple times.

        Returns:
            None
        """
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                logger.debug(f"AcpRunner cleanup: {exc}")
            finally:
                self._exit_stack = None
                self._conn = None
                self._session_id = None
                self._client = None

    async def __aenter__(self) -> "AcpRunner":
        """Enter the async context manager, initializing the ACP runner.

        Returns:
            AcpRunner: This runner instance after initialization.
        """
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        """Exit the async context manager and shut down the agent subprocess.

        Args:
            *_ (Any): Exception type, value, and traceback (ignored).

        Returns:
            None
        """
        await self.cleanup()


# ---------------------------------------------------------------------------
# ClaudeCodeRunner — non-interactive claude -p wrapper
# ---------------------------------------------------------------------------

class ClaudeCodeRunner:
    """
    Non-interactive Claude Code runner using ``claude -p``.

    Spawns ``claude`` as a subprocess with stream-json output so that
    response text can be streamed chunk by chunk.

    Args:
        cwd:      Working directory for the claude process.
        env:      Extra environment variables.
        model:    Override the model (``--model`` flag).
        args:     Extra flags passed verbatim to ``claude``.
    """

    def __init__(
        self,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        args: Optional[List[str]] = None,
    ) -> None:
        """Initialize the Claude Code runner.

        Args:
            cwd (Optional[str]): Working directory for the ``claude`` subprocess.
                Defaults to the current working directory.
            env (Optional[Dict[str, str]]): Extra environment variables merged over
                ``os.environ``. Defaults to None.
            model (Optional[str]): Model override passed as ``--model`` to Claude.
                Defaults to None.
            args (Optional[List[str]]): Extra flags passed verbatim to ``claude``.
                Defaults to None.
        """
        self.cwd = cwd or os.getcwd()
        self.env = env or {}
        self.model = model
        self.args = args or []

    def _build_cmd(self, prompt: str, stream: bool) -> List[str]:
        """Build the ``claude`` CLI command list for a given prompt.

        Args:
            prompt (str): User prompt to pass to ``claude -p``.
            stream (bool): If True, appends ``--output-format stream-json
                --include-partial-messages`` for streaming output.

        Returns:
            List[str]: Complete command list suitable for ``subprocess.exec``.
        """
        cmd = ["claude", "-p", prompt]
        if stream:
            cmd += ["--output-format", "stream-json", "--include-partial-messages"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.args
        return cmd

    async def run(self, prompt: str) -> str:
        """Run a prompt through Claude Code and return the full response text.

        Args:
            prompt (str): User prompt to send to Claude Code.

        Returns:
            str: The complete response text (reasoning chunks excluded).
        """
        chunks: List[str] = []
        async for text, is_reasoning in self.run_stream(prompt):
            if not is_reasoning:
                chunks.append(text)
        return "".join(chunks)

    async def run_stream(self, prompt: str) -> AsyncIterator[Tuple[str, bool]]:
        """Stream (text, is_reasoning) tuples from ``claude -p``.

        Claude Code's stream-json format emits *cumulative* content blocks, so
        delta-slicing is applied: only the newly added text is yielded each event.
        The ``CLAUDECODE`` and ``CLAUDE_CODE_ENTRYPOINT`` environment variables are
        cleared for the subprocess to prevent nested-invocation blocking.

        Args:
            prompt (str): User prompt to send to Claude Code.

        Yields:
            Tuple[str, bool]: ``(text_chunk, is_reasoning)`` pairs.
            ``is_reasoning`` is always ``False`` for Claude Code output.
        """
        cmd = self._build_cmd(prompt, stream=True)
        # Clear Claude Code env vars so nested invocations aren't blocked
        full_env = {**os.environ, **self.env, "CLAUDECODE": "", "CLAUDE_CODE_ENTRYPOINT": ""}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=full_env,
        )

        sent_text_length = 0  # delta-slicing: cumulative content tracker

        assert proc.stdout is not None
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type")

                if evt_type == "assistant":
                    # Cumulative partial message — extract new text via delta-slice
                    message = evt.get("message", {})
                    content_blocks = message.get("content", [])
                    full_text = ""
                    for block in content_blocks:
                        if block.get("type") == "text":
                            full_text += block.get("text", "")
                    if full_text and len(full_text) > sent_text_length:
                        new_text = full_text[sent_text_length:]
                        sent_text_length = len(full_text)
                        yield new_text, False

                elif evt_type == "result":
                    # Final result — emit any remaining text not yet yielded
                    result_text = evt.get("result", "")
                    if result_text and len(result_text) > sent_text_length:
                        yield result_text[sent_text_length:], False
                    break

        finally:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()


# ---------------------------------------------------------------------------
# OpenCodeRunner — ACP runner for opencode acp
# ---------------------------------------------------------------------------

class OpenCodeRunner:
    """ACP runner for OpenCode using ``opencode acp`` (stdio ACP protocol).

    Spawns ``opencode acp`` as an ACP stdio server and communicates with it
    using the standard ACP protocol.  Keeps the subprocess alive across
    multiple calls so session context is preserved.

    Attributes:
        cwd (str): Working directory for the opencode process and ACP session.
        env (Dict[str, str]): Extra environment variables.
        model (Optional[str]): Model override string.
    """

    def __init__(
        self,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        args: Optional[List[str]] = None,
    ) -> None:
        """Initialize the OpenCode runner.

        Args:
            cwd (Optional[str]): Working directory for the opencode process and
                ACP session. Defaults to the current working directory.
            env (Optional[Dict[str, str]]): Extra environment variables merged over
                ``os.environ``. Defaults to None.
            model (Optional[str]): Override the model using
                ``--model provider/model`` syntax. Defaults to None.
            args (Optional[List[str]]): Extra flags passed verbatim to
                ``opencode acp``. Defaults to None.
        """
        self.cwd = cwd or os.getcwd()
        self.env = env or {}
        self.model = model
        self._runner = AcpRunner(
            command="opencode",
            args=["acp"] + (args or []),
            cwd=self.cwd,
            env=self.env,
            model=model,
        )

    @property
    def _session_id(self) -> Optional[str]:
        """Return the current ACP session ID from the underlying AcpRunner.

        Returns:
            Optional[str]: The ACP session ID, or None if not yet initialized.
        """
        return self._runner._session_id

    async def run(self, prompt: str) -> str:
        """Run a prompt through OpenCode and return the full response text.

        Args:
            prompt (str): User prompt to send to OpenCode.

        Returns:
            str: The agent's complete response text (reasoning chunks excluded).
        """
        return await self._runner.run(prompt)

    async def run_stream(self, prompt: str) -> AsyncIterator[Tuple[str, bool]]:
        """Stream (text, is_reasoning) tuples from opencode acp.

        Delegates to the underlying ``AcpRunner.run_stream``.

        Args:
            prompt (str): User prompt to send to OpenCode.

        Yields:
            Tuple[str, bool]: ``(text_chunk, is_reasoning)`` pairs.
        """
        async for chunk in self._runner.run_stream(prompt):
            yield chunk

    async def cleanup(self) -> None:
        """Shut down the opencode subprocess and close the ACP connection.

        Returns:
            None
        """
        await self._runner.cleanup()

    async def __aenter__(self) -> "OpenCodeRunner":
        """Enter the async context manager, initializing the OpenCode runner.

        Returns:
            OpenCodeRunner: This runner instance after initialization.
        """
        await self._runner.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        """Exit the async context manager and shut down the OpenCode process.

        Args:
            *_ (Any): Exception type, value, and traceback (ignored).

        Returns:
            None
        """
        await self.cleanup()
