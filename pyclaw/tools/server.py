"""
pyclaw MCP tool server.

Exposes pyclaw-native tools via the MCP protocol so FastAgent can use them.
Run as: uv run python -m pyclaw.tools.server

Tools provided:
  bash             - shell execution with security policy
  web_search       - DuckDuckGo search (no API key needed)
  send_message     - send to configured channels (Telegram)
  sessions_list    - list active gateway sessions
  sessions_history - get conversation history for a session
  sessions_send    - send a message into another session via gateway API
  sessions_spawn   - spawn a sub-agent session
  memory_search    - search long-term memory
  memory_store     - store a key/value entry in long-term memory
  memory_get       - get a memory entry by key
  memory_delete    - delete a memory entry by key
  memory_list      - list memory keys
  memory_reindex   - rebuild vector search index for all memory entries
  agents_list      - list configured agents
  process          - manage background processes (list/kill)
  image            - image understanding via vision model
  tts              - text-to-speech via MiniMax TTS
  session_status   - current session info
  audit_log_tail   - tail the most recent audit log entries
  audit_log_search - search audit log entries by field/keyword
  workflow_chain   - run a sequential chain of agent steps
  workflow_parallel - run agents in parallel (fan-out/fan-in)
"""
import asyncio
import json
import logging
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP, Context
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

mcp = FastMCP("pyclaw")


# ---------------------------------------------------------------------------
# Tool call logging middleware
# Logs every tool call to the per-agent logger (pyclaw.agent.<name>).
# ---------------------------------------------------------------------------

class _ToolLoggingMiddleware(Middleware):
    """Log MCP tool calls to the per-agent logger."""

    async def on_call_tool(self, context: MiddlewareContext, call_next) -> Any:
        # Resolve calling agent and session from HTTP request headers
        try:
            headers = get_http_headers()
            agent_name = (headers.get("x-agent-name") or "unknown") if headers else "unknown"
            session_id = (headers.get("x-session-id") or "") if headers else ""
        except Exception:
            agent_name = "unknown"
            session_id = ""

        alogger = logging.getLogger(f"pyclaw.agent.{agent_name}")
        prefix = (
            f"[{agent_name}-{session_id.rsplit('-', 1)[-1]}]" if session_id else f"[{agent_name}]"
        )
        tool_name = context.message.name
        args = context.message.arguments or {}

        # Build a brief args preview — truncate long values
        arg_parts = []
        for k, v in args.items():
            v_s = repr(v)
            if len(v_s) > 120:
                v_s = v_s[:117] + "…"
            arg_parts.append(f"{k}={v_s}")
        args_str = ", ".join(arg_parts)

        alogger.info("%s [TOOL] %s(%s)", prefix, tool_name, args_str)
        try:
            result = await call_next(context)
        except Exception as exc:
            alogger.warning("%s [TOOL] %s raised %s: %s", prefix, tool_name, type(exc).__name__, exc)
            raise

        # Extract text preview from ToolResult content blocks
        try:
            content_parts = []
            for block in (result.content if hasattr(result, "content") else []):
                text = getattr(block, "text", None)
                if text:
                    content_parts.append(str(text))
            preview = " ".join(content_parts)[:300].replace("\n", " ")
        except Exception:
            preview = repr(result)[:300]

        alogger.info("%s [TOOL] %s → %s", prefix, tool_name, preview)
        return result


mcp.add_middleware(_ToolLoggingMiddleware())

# ---------------------------------------------------------------------------
# bash / exec
# ---------------------------------------------------------------------------

_SHELL_TIMEOUT = int(os.environ.get("PYCLAW_EXEC_TIMEOUT", "30"))
_EXEC_SECURITY = os.environ.get("PYCLAW_EXEC_SECURITY", "allowlist")  # allowlist|all|none
_SAFE_BINS_ENV = os.environ.get("PYCLAW_SAFE_BINS", "")
_SAFE_BINS: set[str] = (
    set(_SAFE_BINS_ENV.split(",")) if _SAFE_BINS_ENV else set()
)

# Registry of background PIDs started via bash(background=True)
_bg_processes: dict[int, str] = {}

_ALWAYS_BLOCKED = {
    "rm -rf /", "rm -rf /*", ":(){ :|:& };:",  # fork bomb
    "dd if=/dev/random", "mkfs",
}


def _is_safe(command: str) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    cmd_lower = command.strip().lower()

    # Block known destructive patterns
    for blocked in _ALWAYS_BLOCKED:
        if blocked in cmd_lower:
            return False, f"Command matches blocked pattern: {blocked!r}"

    if _EXEC_SECURITY == "all":
        return True, "all mode"

    if _EXEC_SECURITY == "none":
        return False, "exec disabled (security=none)"

    # allowlist mode: check first token against safe_bins
    if _SAFE_BINS:
        try:
            first = shlex.split(command)[0]
        except ValueError:
            first = command.split()[0] if command.split() else ""
        # Accept both full path (/usr/bin/ls) and base name (ls)
        bin_name = Path(first).name
        if first in _SAFE_BINS or bin_name in _SAFE_BINS:
            return True, f"safe bin: {bin_name}"
        return False, f"{bin_name!r} not in safe_bins allowlist"

    # allowlist mode but no safe_bins configured → permit everything
    return True, "no safe_bins restriction"


@mcp.tool()
async def bash(
    command: str,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    background: bool = False,
) -> str:
    """
    Execute a shell command and return its output.

    Args:
        command: Shell command to run (passed to /bin/sh -c)
        cwd: Working directory (defaults to current dir)
        timeout: Seconds before timeout (default 30)
        background: If True, start process and return PID immediately
    """
    allowed, reason = _is_safe(command)
    if not allowed:
        return f"[DENIED] {reason}"

    effective_timeout = timeout or _SHELL_TIMEOUT
    work_dir = cwd or os.getcwd()
    # Strip VIRTUAL_ENV so uv resolves the project environment from the
    # command's working directory rather than inheriting pyclaw's venv.
    clean_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    try:
        if background:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=work_dir,
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _bg_processes[proc.pid] = command
            return f"[BACKGROUND] PID={proc.pid} started: {command}"

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=work_dir,
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"[TIMEOUT] Command exceeded {effective_timeout}s: {command}"

        out = stdout.decode(errors="replace").rstrip()
        err = stderr.decode(errors="replace").rstrip()
        code = proc.returncode

        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if code != 0:
            parts.append(f"[exit {code}]")
        return "\n".join(parts) if parts else f"[exit {code}]"

    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# web_search  (DuckDuckGo, no API key)
# ---------------------------------------------------------------------------

@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 8,
    region: str = "us-en",
) -> str:
    """
    Search the web using DuckDuckGo. Returns titles, URLs, and snippets.

    Args:
        query: Search query
        max_results: Number of results to return (max 20)
        region: Search region (e.g. us-en, uk-en, de-de)
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # fallback

        results = []
        ddg = DDGS()
        for r in ddg.text(query, region=region, max_results=min(max_results, 20)):
            results.append(
                f"**{r.get('title', 'No title')}**\n"
                f"{r.get('href', '')}\n"
                f"{r.get('body', '')}"
            )
        if not results:
            return f"No results found for: {query}"
        return f"Search results for: {query}\n\n" + "\n\n---\n\n".join(results)

    except Exception as e:
        return f"[ERROR] web_search failed: {e}"


# ---------------------------------------------------------------------------
# send_message  (Telegram via bot token in env)
# ---------------------------------------------------------------------------

@mcp.tool()
async def send_message(
    text: str,
    channel: str = "telegram",
    chat_id: Optional[str] = None,
) -> str:
    """
    Send a message to a configured channel (Telegram by default).

    Args:
        text: Message text to send
        channel: Channel name (currently: telegram)
        chat_id: Override chat ID (uses default from config if not provided)
    """
    if channel != "telegram":
        return f"[ERROR] Channel {channel!r} not yet implemented. Use 'telegram'."

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    target_chat_id = chat_id or os.environ.get("PYCLAW_TELEGRAM_CHAT_ID", "")

    if not bot_token:
        return "[ERROR] TELEGRAM_BOT_TOKEN not configured"
    if not target_chat_id:
        return "[ERROR] No chat_id provided and PYCLAW_TELEGRAM_CHAT_ID not set"

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": target_chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if resp.status_code == 200:
                return f"[OK] Message sent to {channel}:{target_chat_id}"
            return f"[ERROR] Telegram API returned {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"[ERROR] send_message failed: {e}"


# ---------------------------------------------------------------------------
# sessions tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def sessions_list() -> str:
    """List all active pyclaw sessions with their agent, channel, and status."""
    try:
        from pyclaw.config.loader import ConfigLoader

        loader = ConfigLoader()
        sessions_dir = Path("~/.pyclaw/sessions").expanduser()
        if not sessions_dir.exists():
            return "No sessions directory found."

        sessions = []
        for f in sorted(sessions_dir.glob("*.json"))[-20:]:  # last 20
            import json
            try:
                data = json.loads(f.read_text())
                sessions.append(
                    f"• {data.get('id', f.stem)[:12]} | "
                    f"agent={data.get('agent_id', '?')} | "
                    f"channel={data.get('channel', '?')} | "
                    f"msgs={len(data.get('messages', []))} | "
                    f"updated={data.get('updated_at', '?')[:16]}"
                )
            except Exception:
                sessions.append(f"• {f.stem} [parse error]")

        return f"Active sessions ({len(sessions)}):\n" + "\n".join(sessions) if sessions else "No sessions found."
    except Exception as e:
        return f"[ERROR] sessions_list failed: {e}"


@mcp.tool()
async def sessions_history(session_id: str, max_messages: int = 20) -> str:
    """
    Get conversation history for a session.

    Args:
        session_id: Session ID (partial match supported)
        max_messages: Max messages to return
    """
    try:
        sessions_dir = Path("~/.pyclaw/sessions").expanduser()
        matches = list(sessions_dir.glob(f"*{session_id}*.json"))
        if not matches:
            return f"No session found matching: {session_id}"

        import json
        data = json.loads(matches[0].read_text())
        messages = data.get("messages", [])[-max_messages:]

        lines = [f"Session: {data.get('id', 'unknown')} | agent: {data.get('agent_id', '?')}"]
        for msg in messages:
            role = msg.get("role", "?").upper()[:9]
            content = msg.get("content", "")[:300]
            lines.append(f"\n[{role}] {content}")

        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] sessions_history failed: {e}"


# ---------------------------------------------------------------------------
# memory tools
# ---------------------------------------------------------------------------

def _agent_memory_service(agent_name: Optional[str] = None):
    """
    Return a memory accessor for the given agent.

    If the gateway has already initialised a MemoryService (in-process mode),
    we create an ephemeral MemoryService that shares the global HookRegistry
    and the configured embedding backend, but uses the agent-specific
    FileMemoryBackend directory.  This ensures that plugin hooks registered
    for ``memory:*`` events are respected and that vector search works.

    When running as a standalone MCP process (no gateway in this process), the
    function falls back to using FileMemoryBackend directly — the interface is
    identical so all call sites work unchanged.
    """
    from pyclaw.memory.file_backend import FileMemoryBackend

    config_dir = os.environ.get("PYCLAW_CONFIG_DIR", "~/.pyclaw")
    name = agent_name or os.environ.get("PYCLAW_AGENT_NAME")
    if not name:
        raise RuntimeError(
            "Cannot access agent memory: no agent name available. "
            "X-Agent-Name header missing from MCP request."
        )
    agent_dir = str(Path(config_dir).expanduser() / "agents" / name)

    # Try to reuse the global hook registry and embedding backend
    embedding_backend = None
    try:
        from pyclaw.memory.service import get_memory_service, MemoryService
        global_svc = get_memory_service()
        if global_svc is not None:
            # Borrow embedding backend from the global service's default backend
            existing = global_svc._default
            if hasattr(existing, "_embedding_backend"):
                embedding_backend = existing._embedding_backend
            backend = FileMemoryBackend(
                base_dir=agent_dir,
                embedding_backend=embedding_backend,
            )
            return MemoryService(
                registry=global_svc._registry,
                default_backend=backend,
            )
    except Exception:
        pass

    return FileMemoryBackend(base_dir=agent_dir)


@mcp.tool()
async def memory_search(ctx: Context, query: str, limit: int = 10) -> str:
    """
    Search long-term memory for relevant context.

    Args:
        query: Search query (keywords)
        limit: Maximum number of results to return
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)
        results = await backend.search(query, limit=limit)
        if not results:
            return "No memory results found."
        lines = []
        for r in results:
            header = f"**{r['key']}** ({r['date']})"
            if r.get("tags"):
                header += f" — tags: {', '.join(r['tags'])}"
            lines.append(header)
            lines.append(r["content"])
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"[ERROR] memory_search failed: {e}"


@mcp.tool()
async def memory_get(ctx: Context, key: str) -> str:
    """
    Get a specific memory entry by key.

    Args:
        key: Memory key to retrieve
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)
        entry = await backend.read(key)
        if entry is None:
            return f"No memory entry found for key: {key}"
        result = f"**{key}** ({entry['date']})\n\n{entry['content']}"
        if entry.get("tags"):
            result += f"\n\nTags: {', '.join(entry['tags'])}"
        return result
    except Exception as e:
        return f"[ERROR] memory_get failed: {e}"


# ---------------------------------------------------------------------------
# session_status
# ---------------------------------------------------------------------------

@mcp.tool()
async def session_status() -> str:
    """Return current gateway status: uptime, active sessions, loaded agents."""
    try:
        import json
        status_file = Path("~/.pyclaw/status.json").expanduser()
        if status_file.exists():
            data = json.loads(status_file.read_text())
            return json.dumps(data, indent=2)
        return "Gateway status unavailable (not running or status file missing)."
    except Exception as e:
        return f"[ERROR] session_status failed: {e}"


# ---------------------------------------------------------------------------
# memory_store / memory_delete / memory_list
# ---------------------------------------------------------------------------

@mcp.tool()
async def memory_store(ctx: Context, key: str, value: str, tags: Optional[str] = None) -> str:
    """
    Store or update a key/value entry in long-term memory.

    Args:
        key: Memory key (e.g. "user-preference-theme")
        value: Content to store
        tags: Optional comma-separated tags for retrieval
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)
        tag_list = [t.strip() for t in tags.split(",")] if tags else []
        await backend.write(key, {"content": value, "tags": tag_list})
        return f"[OK] Stored: {key}"
    except Exception as e:
        return f"[ERROR] memory_store failed: {e}"


@mcp.tool()
async def memory_delete(ctx: Context, key: str) -> str:
    """
    Delete a memory entry by key.

    Args:
        key: Memory key to delete
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)
        deleted = await backend.delete(key)
        if deleted:
            return f"[OK] Deleted: {key}"
        return f"[NOT FOUND] No entry found for key: {key}"
    except Exception as e:
        return f"[ERROR] memory_delete failed: {e}"


@mcp.tool()
async def memory_list(ctx: Context, prefix: str = "") -> str:
    """
    List all memory keys, optionally filtered by prefix.

    Args:
        prefix: Optional key prefix filter
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)
        keys = await backend.list(prefix=prefix)
        if not keys:
            return "No memory entries found."
        return "\n".join(f"- {k}" for k in keys)
    except Exception as e:
        return f"[ERROR] memory_list failed: {e}"


@mcp.tool()
async def memory_reindex(ctx: Context, batch_size: int = 32) -> str:
    """
    Rebuild the vector search index for all memory entries.

    Use this after enabling embeddings on an existing memory directory, or
    after switching embedding models.  Has no effect if no embedding backend
    is configured (returns a message explaining this).

    Args:
        batch_size: Number of entries to embed per API call (default 32)
    """
    try:
        headers = get_http_headers()
        agent_name = headers.get("x-agent-name") if headers else None
        backend = _agent_memory_service(agent_name)

        # Unwrap MemoryService → FileMemoryBackend if needed
        from pyclaw.memory.file_backend import FileMemoryBackend
        fb: Any = backend
        if not isinstance(fb, FileMemoryBackend):
            fb = getattr(fb, "_default", backend)

        if not isinstance(fb, FileMemoryBackend):
            return "[ERROR] memory_reindex requires a FileMemoryBackend"

        result = await fb.reindex(batch_size=batch_size)
        if result["indexed"] == 0 and result["errors"] == 0:
            # No embedding backend configured
            return (
                "No embedding backend configured. "
                "Set memory.embedding.enabled: true in your config to enable vector search."
            )
        return (
            f"[OK] Reindex complete: indexed={result['indexed']} "
            f"errors={result['errors']}"
        )
    except Exception as e:
        return f"[ERROR] memory_reindex failed: {e}"


# ---------------------------------------------------------------------------
# sessions_send
# ---------------------------------------------------------------------------

_GATEWAY_BASE = os.environ.get("PYCLAW_GATEWAY_URL", "http://localhost:8080")


@mcp.tool()
async def sessions_send(
    message: str,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    channel: str = "internal",
) -> str:
    """
    Send a message into another pyclaw session via the gateway API.

    Args:
        message: The message to send
        agent_id: Target agent ID (uses default agent if not specified)
        session_id: Optional existing session ID to send into
        channel: Channel label for the message (default: internal)
    """
    try:
        import httpx

        # Resolve agent_id from config if not provided
        if not agent_id:
            cfg_path = Path("~/.pyclaw/config/pyclaw.yaml").expanduser()
            if cfg_path.exists():
                import yaml  # type: ignore
                cfg = yaml.safe_load(cfg_path.read_text())
                agents = cfg.get("agents", {})
                agent_id = next(iter(agents), "main")
            else:
                agent_id = "main"

        url = f"{_GATEWAY_BASE}/api/v1/agents/{agent_id}/messages"
        payload: dict = {"content": message, "channel": channel}
        if session_id:
            payload["session_id"] = session_id

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                response_text = data.get("response", data.get("content", str(data)))
                return f"[OK] Response: {response_text}"
            return f"[ERROR] Gateway returned {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return f"[ERROR] sessions_send failed: {e}"


# ---------------------------------------------------------------------------
# sessions_spawn
# ---------------------------------------------------------------------------

@mcp.tool()
def subagent_spawn(
    task: str,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    timeout_seconds: int = 300,
    prompt_preset: str = "minimal",
    instruction: Optional[str] = None,
) -> str:
    """
    Spawn a background subagent to handle a task. Returns immediately with a job_id.
    The subagent runs asynchronously; its result is delivered back to your session
    when complete. Use subagents_list() to check status.

    Args:
        task: The task or prompt to give the subagent
        agent: Agent to use (defaults to the calling agent)
        model: Optional model override for the subagent
        timeout_seconds: Max execution time in seconds (default 300)
        prompt_preset: System prompt preset — "minimal" (default), "full", or "task"
        instruction: Optional extra instruction appended to the subagent's system prompt
    """
    try:
        caller = _get_caller_agent()
        target_agent = agent or caller
        if not target_agent:
            return "[ERROR] Cannot determine agent — pass agent= explicitly"

        data = _subagents_api("/", method="POST", json={
            "agent": target_agent,
            "task": task,
            "model": model,
            "timeout_seconds": timeout_seconds,
            "prompt_preset": prompt_preset,
            "instruction": instruction,
        })
        return (
            f"[SUBAGENT SPAWNED]\n"
            f"job_id:     {data['job_id']}\n"
            f"agent:      {data['agent']}\n"
            f"session_id: {data['session_id']}\n"
            f"task:       {data['task']}\n"
            f"Result will be delivered to your session when complete."
        )
    except Exception as e:
        return _fmt_http_err(e) if hasattr(e, "response") else f"[ERROR] {e}"


@mcp.tool()
def subagents_list() -> str:
    """
    List all active subagents spawned by the calling agent.
    Shows job_id, status, task summary, and session_id for each.
    """
    try:
        caller = _get_caller_agent()
        params = f"?agent={caller}" if caller else ""
        data = _subagents_api(f"/{params}")
        agents = data.get("subagents", [])
        if not agents:
            return "No active subagents."
        lines = [f"Active subagents ({len(agents)}):"]
        for s in agents:
            lines.append(
                f"  [{s['status']}] {s['name']}  job_id={s['job_id'][:8]}…  "
                f"agent={s['agent']}  session={s.get('session_id','')[:8]}…"
            )
            lines.append(f"    task: {str(s.get('task',''))[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def subagent_status(job_id: str) -> str:
    """
    Get the current status and details of a subagent.

    Args:
        job_id: The job_id returned by subagent_spawn
    """
    try:
        data = _subagents_api(f"/{job_id}")
        return json.dumps(data, indent=2, default=str)
    except Exception as e:
        return _fmt_http_err(e, job_id)


@mcp.tool()
def subagent_kill(job_id: str) -> str:
    """
    Cancel a running subagent immediately.

    Args:
        job_id: The job_id returned by subagent_spawn
    """
    try:
        _subagents_api(f"/{job_id}", method="DELETE")
        return f"[OK] Subagent {job_id[:8]}… killed."
    except Exception as e:
        return _fmt_http_err(e, job_id)


@mcp.tool()
def subagent_interrupt(job_id: str, task: str) -> str:
    """
    Interrupt a running subagent and restart it with a new task (steer).
    The original subagent is killed and a new one is spawned with the new task,
    preserving the same spawning session for result delivery.

    Args:
        job_id: The job_id of the subagent to interrupt
        task: The new task to give the replacement subagent
    """
    try:
        data = _subagents_api(f"/{job_id}/interrupt", method="POST", json={"task": task})
        return (
            f"[SUBAGENT INTERRUPTED]\n"
            f"old_job_id: {data['old_job_id'][:8]}…\n"
            f"new_job_id: {data['new_job_id']}\n"
            f"new task:   {data['task']}"
        )
    except Exception as e:
        return _fmt_http_err(e, job_id)


@mcp.tool()
def subagent_send(job_id: str, message: str) -> str:
    """
    Queue a follow-up message for a running subagent. The message is processed
    as an additional turn after the subagent's current task completes, using the
    same session. The response is delivered back to your session.

    Args:
        job_id: The job_id of the subagent to send to
        message: The follow-up message or instruction
    """
    try:
        _subagents_api(f"/{job_id}/send", method="POST", json={"message": message})
        return f"[OK] Message queued for subagent {job_id[:8]}…"
    except Exception as e:
        return _fmt_http_err(e, job_id)


# ---------------------------------------------------------------------------
# agents_list
# ---------------------------------------------------------------------------

@mcp.tool()
async def agents_list() -> str:
    """List all configured pyclaw agents with their model and status."""
    try:
        cfg_path = Path("~/.pyclaw/config/pyclaw.yaml").expanduser()
        if not cfg_path.exists():
            return "No pyclaw config found."

        import yaml  # type: ignore
        cfg = yaml.safe_load(cfg_path.read_text())
        agents = cfg.get("agents", {})
        if not agents:
            return "No agents configured."

        lines = [f"Configured agents ({len(agents)}):"]
        for agent_id, agent_cfg in agents.items():
            if not isinstance(agent_cfg, dict):
                continue
            model = agent_cfg.get("model", "?")
            mcp = agent_cfg.get("mcp_servers", [])
            lines.append(
                f"• {agent_id} | model={model} | mcp={mcp}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] agents_list failed: {e}"


# ---------------------------------------------------------------------------
# process  (background process management)
# ---------------------------------------------------------------------------

@mcp.tool()
async def process(
    action: str,
    pid: Optional[int] = None,
    signal: str = "TERM",
) -> str:
    """
    Manage background processes started with bash(background=True).

    Args:
        action: One of: list, kill, status
        pid: Process ID (required for kill/status)
        signal: Signal name for kill: TERM (default), KILL, INT, HUP
    """
    import signal as signal_module

    if action == "list":
        if not _bg_processes:
            return "No tracked background processes."
        lines = ["Background processes:"]
        for p, cmd in list(_bg_processes.items()):
            try:
                os.kill(p, 0)  # check alive
                lines.append(f"• PID={p} running: {cmd[:80]}")
            except ProcessLookupError:
                lines.append(f"• PID={p} exited: {cmd[:80]}")
                _bg_processes.pop(p, None)
        return "\n".join(lines)

    if pid is None:
        return "[ERROR] pid is required for action: " + action

    if action in ("kill", "stop"):
        sig_map = {"TERM": signal_module.SIGTERM, "KILL": signal_module.SIGKILL,
                   "INT": signal_module.SIGINT, "HUP": signal_module.SIGHUP}
        sig = sig_map.get(signal.upper(), signal_module.SIGTERM)
        try:
            os.kill(pid, sig)
            _bg_processes.pop(pid, None)
            return f"[OK] Sent {signal} to PID {pid}"
        except ProcessLookupError:
            return f"[ERROR] No such process: PID {pid}"
        except PermissionError:
            return f"[ERROR] Permission denied to signal PID {pid}"

    if action == "status":
        try:
            os.kill(pid, 0)
            cmd = _bg_processes.get(pid, "unknown command")
            return f"PID {pid}: running | cmd: {cmd}"
        except ProcessLookupError:
            _bg_processes.pop(pid, None)
            return f"PID {pid}: not running (exited)"

    return f"[ERROR] Unknown action: {action!r}. Use: list, kill, status"


# ---------------------------------------------------------------------------
# image  (vision / image understanding)
# ---------------------------------------------------------------------------

@mcp.tool()
async def image(
    path: str,
    prompt: str = "Describe this image in detail.",
) -> str:
    """
    Understand or analyse an image using a vision model (MiniMax VLM).

    Args:
        path: Local file path or URL to the image
        prompt: Question or instruction about the image
    """
    try:
        import base64
        import httpx

        api_key = os.environ.get("GENERIC_API_KEY") or os.environ.get("MINIMAX_API_KEY", "")
        base_url = os.environ.get("GENERIC_BASE_URL")
        if not api_key:
            return "[ERROR] No API key configured (GENERIC_API_KEY / MINIMAX_API_KEY)"
        if not base_url:
            return "[ERROR] No base URL configured (providers.minimax.api_url)"

        # Load image
        if path.startswith("http://") or path.startswith("https://"):
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(path)
                r.raise_for_status()
                image_bytes = r.content
                content_type = r.headers.get("content-type", "image/jpeg").split(";")[0]
        else:
            image_path = Path(path).expanduser()
            if not image_path.exists():
                return f"[ERROR] File not found: {path}"
            image_bytes = image_path.read_bytes()
            suffix = image_path.suffix.lower()
            content_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "png": "image/png", "gif": "image/gif",
                            "webp": "image/webp"}.get(suffix.lstrip("."), "image/jpeg")

        b64 = base64.b64encode(image_bytes).decode()
        data_url = f"data:{content_type};base64,{b64}"

        payload = {
            "model": "MiniMax-VL-01",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 1024,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return text
            return f"[ERROR] Vision API returned {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return f"[ERROR] image tool failed: {e}"


# ---------------------------------------------------------------------------
# tts  (text-to-speech via MiniMax)
# ---------------------------------------------------------------------------

@mcp.tool()
async def tts(
    text: str,
    output_path: Optional[str] = None,
    voice_id: str = "Calm_Woman",
    speed: float = 1.0,
) -> str:
    """
    Convert text to speech using MiniMax TTS and save to a file.

    Args:
        text: Text to convert to speech
        output_path: Where to save the audio file (default: /tmp/tts_<timestamp>.mp3)
        voice_id: Voice ID to use (default: Calm_Woman)
        speed: Speech speed multiplier (0.5 – 2.0, default 1.0)
    """
    try:
        import httpx
        from datetime import datetime as _dt

        api_key = os.environ.get("GENERIC_API_KEY") or os.environ.get("MINIMAX_API_KEY", "")
        if not api_key:
            return "[ERROR] No API key configured (GENERIC_API_KEY / MINIMAX_API_KEY)"

        if not output_path:
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"/tmp/tts_{ts}.mp3"

        payload = {
            "model": "speech-02-hd",
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
            },
        }

        tts_base = os.environ.get("GENERIC_BASE_URL")
        if not tts_base:
            return "[ERROR] No base URL configured (providers.minimax.api_url)"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{tts_base.rstrip('/')}/t2a_v2",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                return f"[ERROR] TTS API returned {resp.status_code}: {resp.text[:300]}"

            data = resp.json()
            audio_hex = data.get("data", {}).get("audio", "")
            if not audio_hex:
                return f"[ERROR] No audio in response: {str(data)[:200]}"

            audio_bytes = bytes.fromhex(audio_hex)
            Path(output_path).write_bytes(audio_bytes)
            size_kb = len(audio_bytes) // 1024
            return f"[OK] Audio saved to {output_path} ({size_kb}KB)"
    except Exception as e:
        return f"[ERROR] tts failed: {e}"


# ---------------------------------------------------------------------------
# Jobs — CRUD via gateway HTTP API
# ---------------------------------------------------------------------------

def _jobs_api(path: str, method: str = "GET", **kwargs) -> dict:
    """Call the jobs HTTP API synchronously."""
    import httpx
    url = f"{_GATEWAY_BASE}/api/v1/jobs{path}"
    with httpx.Client(timeout=15) as client:
        resp = getattr(client, method.lower())(url, **kwargs)
        resp.raise_for_status()
        return resp.json()


def _subagents_api(path: str, method: str = "GET", **kwargs) -> dict:
    """Call the subagents HTTP API synchronously."""
    import httpx
    url = f"{_GATEWAY_BASE}/api/v1/subagents{path}"
    with httpx.Client(timeout=15) as client:
        resp = getattr(client, method.lower())(url, **kwargs)
        resp.raise_for_status()
        return resp.json()


def _fmt_http_err(e: Exception, resource_id: str = "") -> str:
    """Convert an httpx HTTP error to a friendly tool result string."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status == 404:
        suffix = f" '{resource_id}'" if resource_id else ""
        return f"[NOT FOUND]{suffix} — does not exist (may have been deleted)."
    if status == 409:
        return f"[CONFLICT] {e}"
    return f"[ERROR] {e}"


def _get_caller_agent() -> Optional[str]:
    """Read X-Agent-Name from the MCP HTTP request headers."""
    return get_http_headers().get("x-agent-name") or None


@mcp.tool()
def jobs_list(all_agents: bool = False) -> str:
    """
    List scheduled jobs with status, next run time, and run counts.

    By default shows only jobs owned by the calling agent.
    Set all_agents=True to list every job across all agents.
    """
    try:
        agent = _get_caller_agent()
        if all_agents or not agent:
            data = _jobs_api("/")
        else:
            data = _jobs_api(f"/?owner={agent}")
        jobs = data.get("jobs", [])
        if not jobs:
            return "No jobs scheduled."
        lines = [f"Scheduled jobs ({len(jobs)}):"]
        for j in jobs:
            icon = "✅" if j.get("enabled") else "⏸"
            run = j.get("run", {})
            sched = j.get("schedule", {})
            sched_str = sched.get("expr") or (f"{sched.get('seconds')}s" if sched.get("seconds") else sched.get("at", "?"))
            lines.append(
                f"  {icon} {j['name']} [{run.get('kind','?')}] "
                f"schedule={sched_str} next={j.get('next_run','—')} runs={j.get('run_count',0)}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def jobs_get(job: str) -> str:
    """
    Get full details of a job by name or ID.

    Args:
        job: Job name or ID
    """
    try:
        return json.dumps(_jobs_api(f"/{job}"), indent=2, default=str)
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_create_command(
    name: str,
    schedule: str,
    command: str,
    description: str = "",
    timeout_seconds: int = 300,
    deliver_channel: str = "",
    deliver_chat_id: str = "",
) -> str:
    """
    Create a scheduled shell command job.

    Args:
        name: Unique job name (e.g. "cleanup-tmp")
        schedule: When to run:
                  "0 9 * * *"               cron — 9am daily UTC
                  "0 9 * * * America/New_York"  cron with timezone
                  "30m" / "2h" / "1d"       fixed interval
                  "2026-03-10T09:00:00Z"    one-shot datetime
        command: Shell command to execute
        description: Optional description
        timeout_seconds: Max execution time in seconds (default 300)
        deliver_channel: "telegram", "slack", or "" for gateway default
        deliver_chat_id: Specific recipient ID (optional)
    """
    try:
        data = _jobs_api("/command", method="POST", json={
            "name": name, "schedule": schedule, "command": command,
            "description": description or None, "timeout_seconds": timeout_seconds,
            "deliver_channel": deliver_channel or None,
            "deliver_chat_id": deliver_chat_id or None,
            "agent": _get_caller_agent() or "",
        })
        j = data.get("job", {})
        return f"[OK] Created command job '{j.get('name')}' (id={j.get('id')}) next_run={j.get('next_run')}"
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def jobs_create_agent(
    name: str,
    schedule: str,
    agent: str,
    message: str,
    model: str = "",
    description: str = "",
    timeout_seconds: int = 300,
    deliver_channel: str = "",
    deliver_chat_id: str = "",
) -> str:
    """
    Create a scheduled agent job that sends a prompt to an agent and delivers its response.

    Args:
        name: Unique job name (e.g. "daily-news")
        schedule: When to run:
                  "0 8 * * *"               cron — 8am daily UTC
                  "0 8 * * * America/New_York"  cron with timezone
                  "1h"                      every hour
                  "2026-03-10T09:00:00Z"    one-shot
        agent: Agent name from config (e.g. "assistant")
        message: Prompt to send to the agent each run
        model: Optional model override, empty = use agent default
        description: Optional description
        timeout_seconds: Max agent response time (default 300)
        deliver_channel: "telegram", "slack", or "" for gateway default
        deliver_chat_id: Specific recipient ID (optional)
    """
    try:
        data = _jobs_api("/agent", method="POST", json={
            "name": name, "schedule": schedule, "agent": agent,
            "message": message, "model": model or None,
            "description": description or None, "timeout_seconds": timeout_seconds,
            "deliver_channel": deliver_channel or None,
            "deliver_chat_id": deliver_chat_id or None,
        })
        j = data.get("job", {})
        return f"[OK] Created agent job '{j.get('name')}' (id={j.get('id')}) next_run={j.get('next_run')}"
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def jobs_update(
    job: str,
    schedule: str = "",
    enabled: str = "",
    timeout_seconds: int = 0,
    deliver_channel: str = "",
    deliver_chat_id: str = "",
    report_to_agent: str = "",
    deliver_none: bool = False,
) -> str:
    """
    Update a job's schedule, enabled state, or delivery config.

    Args:
        job: Job name or ID
        schedule: New schedule string (empty = keep current)
        enabled: "true" or "false" (empty = keep current)
        timeout_seconds: New timeout in seconds (0 = keep current)
        deliver_channel: New delivery channel (empty = keep current)
        deliver_chat_id: New delivery chat ID (empty = keep current)
        report_to_agent: Agent name to deliver results to active session (empty = keep current, "none" = clear)
        deliver_none: Set to true to suppress all delivery notifications
    """
    try:
        payload: dict = {}
        if schedule:
            payload["schedule"] = schedule
        if enabled in ("true", "false"):
            payload["enabled"] = enabled == "true"
        if timeout_seconds > 0:
            payload["timeout_seconds"] = timeout_seconds
        if deliver_channel:
            payload["deliver_channel"] = deliver_channel
        if deliver_chat_id:
            payload["deliver_chat_id"] = deliver_chat_id
        if report_to_agent:
            payload["report_to_agent"] = None if report_to_agent == "none" else report_to_agent
        if deliver_none:
            payload["deliver_none"] = True
        if not payload:
            return "[ERROR] No update fields provided."
        data = _jobs_api(f"/{job}", method="PATCH", json=payload)
        j = data.get("job", {})
        return f"[OK] Updated '{j.get('name')}' next_run={j.get('next_run')}"
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_delete(job: str) -> str:
    """
    Permanently delete a scheduled job.

    System jobs (names starting and ending with __) cannot be deleted.
    Use jobs_enable or jobs_disable to control them instead.

    Args:
        job: Job name or ID
    """
    if job.startswith("__") and job.endswith("__"):
        return f"[FORBIDDEN] System job '{job}' cannot be deleted. Use jobs_enable or jobs_disable to control it."
    try:
        data = _jobs_api(f"/{job}", method="DELETE")
        return f"[OK] Deleted job '{data.get('deleted')}'"
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_enable(job: str) -> str:
    """
    Enable a disabled job.

    Args:
        job: Job name or ID
    """
    try:
        data = _jobs_api(f"/{job}/enable", method="POST")
        return f"[OK] Enabled '{data.get('job')}' next_run={data.get('next_run')}"
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_disable(job: str) -> str:
    """
    Disable a job without deleting it.

    Args:
        job: Job name or ID
    """
    try:
        data = _jobs_api(f"/{job}/disable", method="POST")
        return f"[OK] Disabled '{data.get('job')}'"
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_run_now(job: str) -> str:
    """
    Trigger a job to run immediately regardless of schedule.

    Args:
        job: Job name or ID
    """
    try:
        data = _jobs_api(f"/{job}/run", method="POST")
        return f"[OK] Triggered '{data.get('job')}'"
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_history(job: str, limit: int = 10) -> str:
    """
    Get recent run history for a job.

    Args:
        job: Job name or ID
        limit: Number of recent runs to show (default 10)
    """
    try:
        data = _jobs_api(f"/{job}/history?limit={limit}")
        runs = data.get("runs", [])
        if not runs:
            return f"No run history for '{data.get('job_name', job)}'."
        lines = [f"History for '{data.get('job_name')}' (last {len(runs)}):"]
        for r in runs:
            icon = "✅" if r.get("status") == "completed" else "❌"
            dur = f"{r.get('duration_ms') or 0:.0f}ms"
            started = (r.get("started_at") or "")[:19]
            lines.append(f"  {icon} {started} ({dur})")
            if r.get("error"):
                lines.append(f"      error: {r['error'][:120]}")
            elif r.get("stdout"):
                lines.append(f"      output: {r['stdout'].strip()[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return _fmt_http_err(e, job)


@mcp.tool()
def jobs_status() -> str:
    """Get overall job scheduler status (total, enabled, running counts)."""
    try:
        return json.dumps(_jobs_api("/status"), indent=2)
    except Exception as e:
        return f"[ERROR] {e}"


# ---------------------------------------------------------------------------
# TODOs — CRUD via gateway HTTP API
# ---------------------------------------------------------------------------

def _todos_api(path: str, method: str = "GET", **kwargs) -> dict:
    """Call the todos HTTP API synchronously."""
    import httpx
    url = f"{_GATEWAY_BASE}/api/v1/todos{path}"
    with httpx.Client(timeout=15) as client:
        resp = getattr(client, method.lower())(url, **kwargs)
        resp.raise_for_status()
        return resp.json()


def _fmt_todo(t: dict) -> str:
    priority = t.get("priority", "?").upper()
    status = t.get("status", "?")
    due = f" due={t['due_date'][:10]}" if t.get("due_date") else ""
    tags = f" [{','.join(t['tags'])}]" if t.get("tags") else ""
    owner = f" @{t['owner']}" if t.get("owner") else ""
    notes = f"\n    notes: {t['notes'][:80]}" if t.get("notes") else ""
    blocked = f" blocked_by={t['blocked_by']}" if t.get("blocked_by") else ""
    return (
        f"[{t['id']}] [{priority}] [{status}] {t['title']}"
        f"{due}{tags}{owner}{blocked}{notes}"
    )


@mcp.tool()
def todos_list(
    status: str = "",
    priority: str = "",
    tags: str = "",
    all_agents: bool = False,
) -> str:
    """
    List TODO items with optional filters.

    By default shows todos owned by the calling agent plus human-created ones.
    Set all_agents=True to see all todos regardless of owner.

    Args:
        status:     Filter by status: open, in_progress, done, cancelled, blocked
        priority:   Filter by priority: low, medium, high, critical
        tags:       Comma-separated tags to filter by
        all_agents: If True, include todos from all agents
    """
    try:
        agent = _get_caller_agent()
        params: dict = {"all_owners": all_agents}
        if not all_agents and agent:
            params["owner"] = agent
        if status:
            params["status"] = status
        if priority:
            params["priority"] = priority
        if tags:
            params["tags"] = tags
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        data = _todos_api(f"/?{qs}" if qs else "/")
        todos = data.get("todos", [])
        if not todos:
            return "No todos found."
        lines = [f"Todos ({len(todos)}):"]
        for t in todos:
            lines.append("  " + _fmt_todo(t))
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def todo_get(todo_id: str) -> str:
    """
    Get full details of a TODO by ID.

    Args:
        todo_id: The 8-character TODO ID
    """
    try:
        data = _todos_api(f"/{todo_id}")
        return json.dumps(data.get("todo", data), indent=2, default=str)
    except Exception as e:
        return _fmt_http_err(e, todo_id)


@mcp.tool()
def todo_create(
    title: str,
    description: str = "",
    priority: str = "medium",
    tags: str = "",
    due_date: str = "",
    blocked_by: str = "",
) -> str:
    """
    Create a new TODO item. Ownership is set automatically from the calling agent.

    Args:
        title:       Short title for the task
        description: Detailed description (optional)
        priority:    low | medium | high | critical  (default: medium)
        tags:        Comma-separated tags, e.g. "infra,urgent"
        due_date:    Optional deadline in ISO format: "2026-04-01" or "2026-04-01T09:00:00"
        blocked_by:  ID of another TODO this depends on (optional)
    """
    try:
        payload: dict = {
            "title": title,
            "description": description or None,
            "priority": priority,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "owner": _get_caller_agent(),
        }
        if due_date:
            payload["due_date"] = due_date
        if blocked_by:
            payload["blocked_by"] = blocked_by
        data = _todos_api("/", method="POST", json=payload)
        t = data.get("todo", {})
        return f"[OK] Created todo [{t.get('id')}] {t.get('title')} (priority={t.get('priority')})"
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def todo_update(
    todo_id: str,
    title: str = "",
    description: str = "",
    priority: str = "",
    tags: str = "",
    due_date: str = "",
    blocked_by: str = "",
    notes: str = "",
) -> str:
    """
    Update fields on a TODO. Only provided (non-empty) fields are changed.

    Args:
        todo_id:     The 8-character TODO ID
        title:       New title
        description: New description
        priority:    low | medium | high | critical
        tags:        New comma-separated tags (replaces existing)
        due_date:    New deadline in ISO format, or "none" to clear
        blocked_by:  Dependency TODO ID, or "none" to clear
        notes:       Progress or context notes
    """
    try:
        payload: dict = {}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if priority:
            payload["priority"] = priority
        if tags:
            payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if due_date:
            payload["due_date"] = None if due_date.lower() == "none" else due_date
        if blocked_by:
            payload["blocked_by"] = None if blocked_by.lower() == "none" else blocked_by
        if notes:
            payload["notes"] = notes
        if not payload:
            return "[ERROR] No fields provided to update."
        data = _todos_api(f"/{todo_id}", method="PATCH", json=payload)
        t = data.get("todo", {})
        return f"[OK] Updated [{t.get('id')}] {t.get('title')}"
    except Exception as e:
        return _fmt_http_err(e, todo_id)


@mcp.tool()
def todo_mark(todo_id: str, status: str, notes: str = "") -> str:
    """
    Set the status of a TODO.

    Args:
        todo_id: The 8-character TODO ID
        status:  New status: open | in_progress | done | cancelled | blocked
        notes:   Optional note explaining the transition (e.g. completion summary,
                 reason for blocking/cancellation)
    """
    try:
        payload: dict = {"status": status}
        if notes:
            payload["notes"] = notes
        data = _todos_api(f"/{todo_id}/mark", method="POST", json=payload)
        t = data.get("todo", {})
        return f"[OK] Marked [{t.get('id')}] {t.get('title')} → {t.get('status')}"
    except Exception as e:
        return _fmt_http_err(e, todo_id)


@mcp.tool()
def todo_delete(todo_id: str) -> str:
    """
    Permanently delete a TODO.

    Args:
        todo_id: The 8-character TODO ID
    """
    try:
        data = _todos_api(f"/{todo_id}", method="DELETE")
        return f"[OK] Deleted todo '{data.get('deleted')}'"
    except Exception as e:
        return _fmt_http_err(e, todo_id)


@mcp.tool()
def todos_next(all_agents: bool = False) -> str:
    """
    Return the oldest highest-priority open unblocked TODO to work on next.

    Todos are ranked by priority (critical → high → medium → low) then by
    creation time ascending — so the oldest item at each priority level is
    returned first.  Blocked todos (where the dependency is still open) are
    skipped automatically.

    By default scoped to the calling agent plus human-created todos.
    Set all_agents=True to consider todos from all agents.
    """
    try:
        agent = _get_caller_agent()
        params: dict = {"all_owners": all_agents}
        if not all_agents and agent:
            params["owner"] = agent
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        data = _todos_api(f"/next?{qs}" if qs else "/next")
        t = data.get("todo")
        if not t:
            return data.get("message", "No open unblocked todos found.")
        return (
            f"Next todo:\n  " + _fmt_todo(t) +
            f"\n\nDescription: {t.get('description') or '(none)'}"
        )
    except Exception as e:
        return f"[ERROR] {e}"


# ---------------------------------------------------------------------------
# Skills — discover and read agent skills (agentskills.io format)
# ---------------------------------------------------------------------------

def _discover_all_skills(caller: Optional[str] = None) -> list:
    """Discover skills from global dir + all agents' skill dirs (deduped, caller wins)."""
    from pyclaw.skills.registry import discover_skills, get_skill_dirs, _parse_skill_dir
    import os as _os
    from pathlib import Path
    config_dir = Path(_os.path.expanduser("~/.pyclaw"))
    # Build ordered list: global, then other agents, then caller last (wins on conflict)
    dirs = list(get_skill_dirs(None))  # global only first
    agents_root = config_dir / "agents"
    if agents_root.exists():
        for agent_dir in sorted(agents_root.iterdir()):
            if agent_dir.is_dir() and agent_dir.name != (caller or ""):
                dirs.extend(get_skill_dirs(agent_dir.name))
    if caller:
        dirs.extend(get_skill_dirs(caller))  # caller last so it overrides
    seen: dict = {}
    for skill_dir in dirs:
        if not skill_dir.exists():
            continue
        for entry in sorted(skill_dir.iterdir()):
            if entry.is_dir():
                skill = _parse_skill_dir(entry)
                if skill:
                    seen[skill.name.lower()] = skill
    return list(seen.values())


@mcp.tool()
def skills_list(all_agents: bool = False) -> str:
    """
    List all available skills with their names and descriptions.

    By default, lists skills for the calling agent (global + agent-specific).
    Set all_agents=True to include skills from ALL agents, not just the caller.

    Use skill_read(name) to get the full instructions for a specific skill.
    """
    try:
        from pyclaw.skills.registry import discover_skills
        caller = _get_caller_agent()
        if all_agents or caller is None:
            # No caller identified — search all agents so nothing is hidden
            skills = _discover_all_skills(caller)
        else:
            skills = discover_skills(agent_name=caller)
        if not skills:
            return "No skills installed. Add skill directories to ~/.pyclaw/skills/."
        lines = [f"Available skills ({len(skills)}):"]
        for s in sorted(skills, key=lambda x: x.name):
            ver = f" v{s.version}" if s.version else ""
            lines.append(f"  • {s.name}{ver} — {s.description}")
            if s.allowed_tools:
                lines.append(f"    allowed-tools: {', '.join(s.allowed_tools)}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] skills_list failed: {e}"


@mcp.tool()
def skill_read(name: str) -> str:
    """
    Read the full SKILL.md for a skill by name.

    The content includes instructions, usage examples, and script references.
    Script paths use the absolute path to the skill directory so you can run
    them directly with the bash tool.

    Args:
        name: Skill name (case-insensitive, as listed by skills_list)
    """
    try:
        from pyclaw.skills.registry import find_skill
        agent = _get_caller_agent()
        skill = find_skill(name, agent_name=agent)
        if skill is None and agent is not None:
            # Caller identified but skill not in their dirs — try global fallback
            skill = find_skill(name)
        if skill is None:
            # No caller or still not found — search all agents
            key = name.lower().strip()
            for s in _discover_all_skills(agent):
                if s.name.lower() == key:
                    skill = s
                    break
        if skill is None:
            return f"[ERROR] Skill {name!r} not found. Use skills_list() to see available skills."
        return skill.read_content()
    except Exception as e:
        return f"[ERROR] skill_read failed: {e}"


# ---------------------------------------------------------------------------
# Config — CRUD on pyclaw.yaml, validate, reload
# ---------------------------------------------------------------------------

def _find_config_path() -> Optional[Path]:
    """Locate the active pyclaw config file (same search order as ConfigLoader)."""
    from pyclaw.config.loader import DEFAULT_CONFIG_PATHS, expand_path
    for p in DEFAULT_CONFIG_PATHS:
        path = expand_path(p)
        if path.exists():
            return path
    return None


def _ruamel_load(path: Path):
    """Load YAML preserving comments via ruamel.yaml. Returns (yaml_instance, data)."""
    from ruamel.yaml import YAML
    ry = YAML()
    ry.preserve_quotes = True
    with open(path, "r") as f:
        data = ry.load(f)
    return ry, data or {}


def _ruamel_save(ry, data, path: Path) -> None:
    """Write back YAML via ruamel.yaml (comments preserved)."""
    import io
    buf = io.StringIO()
    ry.dump(data, buf)
    path.write_text(buf.getvalue())


def _split_path(path: str) -> list[str]:
    """Split a dot-notation config path into key segments.

    Bracket notation ``[key]`` is supported for keys that contain dots
    (e.g. model names like ``MiniMax-M2.5``)::

        providers.minimax.models.[MiniMax-M2.5].concurrency
        → ['providers', 'minimax', 'models', 'MiniMax-M2.5', 'concurrency']

    Plain segments must not contain dots; bracket segments may contain anything
    except ``]``.
    """
    import re
    parts = []
    for m in re.finditer(r'\[([^\]]+)\]|([^.\[\]]+)', path):
        parts.append(m.group(1) if m.group(1) is not None else m.group(2))
    return [p for p in parts if p]


def _nav_path(data: dict, parts: list[str], *, create: bool = False):
    """Walk dot-notation path into nested dict. Returns (parent, last_key)."""
    node = data
    for part in parts[:-1]:
        if part not in node:
            if not create:
                raise KeyError(f"Key not found: {part!r}")
            node[part] = {}
        node = node[part]
        if not isinstance(node, dict):
            raise TypeError(f"Cannot descend into non-dict at {part!r}")
    return node, parts[-1]


def _parse_value(raw: str) -> Any:
    """Try JSON parse, fall back to string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _config_api(path: str, method: str = "GET", **kwargs) -> dict:
    """Call the config HTTP API synchronously."""
    import httpx
    url = f"{_GATEWAY_BASE}/api/v1/config{path}"
    with httpx.Client(timeout=15) as client:
        resp = getattr(client, method.lower())(url, **kwargs)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def config_get() -> str:
    """
    Return the current pyclaw configuration (sensitive fields redacted).
    Reads live config from the running gateway.
    """
    try:
        data = _config_api("/")
        return json.dumps(data.get("config", data), indent=2)
    except Exception as e:
        return f"[ERROR] config_get failed: {e}"


@mcp.tool()
def config_set(path: str, value: str) -> str:
    """
    Set a config value using dot-notation path and reload (where hot-reloadable).

    Hot-reloadable (no restart): system_prompt, model, temperature, max_tokens,
      job schedules, security exec_approvals mode.
    Requires restart: new agents, host/port, bot tokens, mcp_port.

    Args:
        path:  Dot-notation key path, e.g. "agents.assistant.model"
               or "gateway.port" or "agents.assistant.tools.profile".
               Use bracket notation for keys that contain dots (e.g. model names):
                 "providers.minimax.models.[MiniMax-M2.5].concurrency"
        value: New value as JSON or a plain string.
               Use JSON for booleans (true/false), numbers, lists, dicts.
               Examples:
                 "claude-opus-4-6"           → string
                 "true" / "false"            → boolean
                 "2048"                      → integer
                 '["pyclaw","fetch"]'        → list
    """
    try:
        cfg_path = _find_config_path()
        if cfg_path is None:
            return "[ERROR] No pyclaw config file found."

        ry, data = _ruamel_load(cfg_path)
        parts = _split_path(path)
        if not parts:
            return "[ERROR] Empty path."

        parent, last_key = _nav_path(data, parts, create=True)
        parsed = _parse_value(value)
        parent[last_key] = parsed

        # Validate before writing
        import yaml as _yaml
        import io
        buf = io.StringIO()
        ry.dump(data, buf)
        raw_dict = _yaml.safe_load(buf.getvalue()) or {}
        from pyclaw.config.schema import Config
        Config(**raw_dict)  # raises ValidationError if invalid

        _ruamel_save(ry, data, cfg_path)
        return f"[OK] Set {path} = {parsed!r} in {cfg_path}"
    except Exception as e:
        return f"[ERROR] config_set failed: {e}"


@mcp.tool()
def config_delete(path: str) -> str:
    """
    Delete a config key using dot-notation path.

    Args:
        path: Dot-notation key path, e.g. "agents.old_agent".
              Use bracket notation for keys containing dots:
                "providers.minimax.models.[MiniMax-M2.5]"
    """
    try:
        cfg_path = _find_config_path()
        if cfg_path is None:
            return "[ERROR] No pyclaw config file found."

        ry, data = _ruamel_load(cfg_path)
        parts = _split_path(path)
        if not parts:
            return "[ERROR] Empty path."

        parent, last_key = _nav_path(data, parts)
        if last_key not in parent:
            return f"[ERROR] Key not found: {path!r}"
        del parent[last_key]

        # Validate before writing
        import yaml as _yaml
        import io
        buf = io.StringIO()
        ry.dump(data, buf)
        raw_dict = _yaml.safe_load(buf.getvalue()) or {}
        from pyclaw.config.schema import Config
        Config(**raw_dict)  # raises ValidationError if invalid

        _ruamel_save(ry, data, cfg_path)
        return f"[OK] Deleted {path!r} from {cfg_path}"
    except Exception as e:
        return f"[ERROR] config_delete failed: {e}"


@mcp.tool()
def config_validate() -> str:
    """
    Validate the current config file against the schema without reloading.
    Returns 'valid' or describes what is wrong.
    """
    try:
        cfg_path = _find_config_path()
        if cfg_path is None:
            return "[ERROR] No pyclaw config file found."

        import yaml as _yaml
        raw = _yaml.safe_load(cfg_path.read_text()) or {}
        from pyclaw.config.schema import Config
        Config(**raw)
        return f"[OK] Config is valid: {cfg_path}"
    except Exception as e:
        return f"[INVALID] {e}"


@mcp.tool()
def config_reload() -> str:
    """
    Reload configuration from disk and apply all hot-reloadable changes.
    Hot-reloadable: model, temperature, max_tokens, system_prompt,
    security exec_approvals mode.  Changes to host/port/tokens need a restart.
    """
    try:
        data = _config_api("/reload", method="post")
        changed = data.get("changed", [])
        if changed:
            return f"[OK] Reloaded. Changed: {', '.join(str(c) for c in changed)}"
        return "[OK] Reloaded. No changes detected."
    except Exception as e:
        return f"[ERROR] config_reload failed: {e}"


@mcp.tool()
def config_schema(section: str = "") -> str:
    """
    Return the JSON schema for the pyclaw configuration (or a specific section).
    Useful for understanding what fields are available before calling config_set.

    Args:
        section: Optional top-level section name, e.g. "gateway", "agents",
                 "security", "sessions". Leave empty for the full schema.
    """
    try:
        from pyclaw.config.schema import Config
        full_schema = Config.model_json_schema()

        if not section:
            return json.dumps(full_schema, indent=2)

        # Try to find the section in $defs or properties
        defs = full_schema.get("$defs", {})
        props = full_schema.get("properties", {})

        if section in props:
            ref = props[section]
            # Resolve $ref if present
            if "$ref" in ref:
                def_name = ref["$ref"].split("/")[-1]
                return json.dumps(defs.get(def_name, ref), indent=2)
            return json.dumps(ref, indent=2)

        # Search $defs directly by name (case-insensitive)
        for name, schema_def in defs.items():
            if name.lower() == section.lower() or name.lower().startswith(section.lower()):
                return json.dumps(schema_def, indent=2)

        available = list(props.keys())
        return f"[ERROR] Section {section!r} not found. Available: {available}"
    except Exception as e:
        return f"[ERROR] config_schema failed: {e}"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit_log_path() -> Optional[Path]:
    """Return the audit log path from config or default."""
    cfg_path = _find_config_path()
    if cfg_path is not None:
        try:
            import yaml as _yaml
            raw = _yaml.safe_load(cfg_path.read_text()) or {}
            log_file = (
                raw.get("security", {})
                   .get("audit", {})
                   .get("log_file", "~/.pyclaw/logs/audit.log")
            )
            return Path(os.path.expanduser(log_file))
        except Exception:
            pass
    return Path(os.path.expanduser("~/.pyclaw/logs/audit.log"))


@mcp.tool()
def audit_log_tail(n: int = 50) -> str:
    """
    Return the last N entries from the audit log (newest last).

    Args:
        n: Number of entries to return (default 50, max 500).
    """
    n = min(max(1, n), 500)
    log_path = _audit_log_path()
    if log_path is None or not log_path.exists():
        return "[INFO] Audit log not found or not yet created."
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        tail = [l for l in lines if l.strip()][-n:]
        entries = []
        for line in tail:
            try:
                e = json.loads(line)
                entries.append(e)
            except json.JSONDecodeError:
                entries.append({"raw": line})
        return json.dumps(entries, indent=2)
    except Exception as exc:
        return f"[ERROR] audit_log_tail failed: {exc}"


@mcp.tool()
def audit_log_search(
    keyword: str = "",
    event_type: str = "",
    agent_id: str = "",
    session_id: str = "",
    status: str = "",
    limit: int = 100,
) -> str:
    """
    Search audit log entries. All filters are ANDed together; omit any to skip that filter.

    Args:
        keyword:    Case-insensitive substring match against the raw JSON line.
        event_type: Filter by event_type field (e.g. "message_received", "tool_execution").
        agent_id:   Filter by agent_id field.
        session_id: Filter by session_id field.
        status:     Filter by status field (e.g. "success", "denied", "error").
        limit:      Maximum number of matching entries to return (default 100, max 500).
    """
    limit = min(max(1, limit), 500)
    log_path = _audit_log_path()
    if log_path is None or not log_path.exists():
        return "[INFO] Audit log not found or not yet created."
    try:
        results = []
        kw_lower = keyword.lower() if keyword else None
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if kw_lower and kw_lower not in line.lower():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and e.get("event_type") != event_type:
                    continue
                if agent_id and e.get("agent_id") != agent_id:
                    continue
                if session_id and e.get("session_id") != session_id:
                    continue
                if status and e.get("status") != status:
                    continue
                results.append(e)
                if len(results) >= limit:
                    break
        return json.dumps({"total": len(results), "entries": results}, indent=2)
    except Exception as exc:
        return f"[ERROR] audit_log_search failed: {exc}"


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

@mcp.tool()
async def workflow_chain(
    steps: str,
    input: str,
    model: str = "sonnet",
) -> str:
    """
    Run a sequential chain workflow where each step's output feeds the next.

    Args:
        steps:  JSON array of step objects. Each step must have:
                  "name"        - step identifier
                  "instruction" - system prompt for the agent at this step
                Optional per step:
                  "agent_name"  - reuse an existing named agent (default: step name)
                  "output_key"  - context key to store result under (default: "output")
                Example:
                  [{"name":"draft","instruction":"Write a blog post draft about: "},
                   {"name":"edit","instruction":"Improve clarity and fix grammar in: "}]
        input:  Initial input passed to the first step.
        model:  Model string for all steps (default "sonnet").
    """
    try:
        steps_list = json.loads(steps)
        if not isinstance(steps_list, list) or not steps_list:
            return "[ERROR] steps must be a non-empty JSON array."
    except json.JSONDecodeError as exc:
        return f"[ERROR] Could not parse steps JSON: {exc}"

    try:
        from pyclaw.workflows.chain import run_chain
        result = await run_chain(
            steps=steps_list,
            initial_input=input,
            model=model,
        )
        return str(result)
    except Exception as exc:
        return f"[ERROR] workflow_chain failed: {exc}"


@mcp.tool()
async def workflow_parallel(
    agents: str,
    input: str,
    model: str = "sonnet",
    fan_in_instruction: str = "",
    max_concurrent: int = 5,
) -> str:
    """
    Run agents in parallel over the same input, then optionally aggregate results.

    Args:
        agents:             JSON array of agent objects. Each must have:
                              "name"        - agent identifier
                              "instruction" - system prompt for this agent
                            Optional per agent:
                              "agent_name"  - reuse an existing named agent
                            Example:
                              [{"name":"pros","instruction":"List the pros of: "},
                               {"name":"cons","instruction":"List the cons of: "}]
        input:              Input passed to every parallel agent.
        model:              Model string for all agents (default "sonnet").
        fan_in_instruction: If non-empty, a final aggregation agent runs with this
                            instruction over all parallel results.  Leave empty to
                            return raw results as a JSON object.
        max_concurrent:     Maximum number of agents to run simultaneously (default 5).
    """
    try:
        agents_list = json.loads(agents)
        if not isinstance(agents_list, list) or not agents_list:
            return "[ERROR] agents must be a non-empty JSON array."
    except json.JSONDecodeError as exc:
        return f"[ERROR] Could not parse agents JSON: {exc}"

    try:
        from pyclaw.workflows.parallel import run_parallel
        fan_in = {"instruction": fan_in_instruction} if fan_in_instruction else None
        result = await run_parallel(
            agents=agents_list,
            input_data=input,
            fan_in=fan_in,
            model=model,
            max_concurrent=max_concurrent,
        )
        if isinstance(result, (list, dict)):
            return json.dumps(result, indent=2, default=str)
        return str(result)
    except Exception as exc:
        return f"[ERROR] workflow_parallel failed: {exc}"


# ---------------------------------------------------------------------------
# Entry point — transport controlled by PYCLAW_MCP_TRANSPORT env var
#   streamable-http (default): HTTP server on PYCLAW_MCP_PORT (default 8081)
#   stdio: stdio transport for direct subprocess use (e.g. tests)
# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@mcp.tool()
async def secrets_list() -> str:
    """
    List all secret names registered in the pyclaw secrets registry.

    Returns the names and their source types — never the actual values.
    Use secret_get() to resolve a specific value.
    """
    try:
        r = await _config_api("secrets")
        return json.dumps(r, indent=2)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
async def secret_get(name: str) -> str:
    """
    Retrieve a named secret value from the pyclaw secrets registry.

    The secret must be registered under 'secrets:' in pyclaw.yaml.

    Args:
        name: Registered secret name (e.g. AV_KEY, ALPACA_KEY, MINIMAX_API_KEY).
    """
    try:
        r = await _config_api(f"secrets/{name}")
        return r.get("value", "")
    except Exception as e:
        return f"[ERROR] {e}"


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("PYCLAW_MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        port = int(os.environ.get("PYCLAW_MCP_PORT", "8081"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
