"""Main Gateway class for pyclopse."""

import asyncio
from pyclopse.reflect import reflect_system
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pyclopse.config.loader import ConfigLoader, Config
from pyclopse.config.schema import AgentConfig, SecurityConfig
from pyclopse.hooks.events import HookEvent
from pyclopse.security.audit import AuditLogger
from pyclopse.security.approvals import ExecApprovalSystem
from pyclopse.security.sandbox import Sandbox, create_sandbox
from pyclopse.jobs.scheduler import JobScheduler
from pyclopse.core.agent import Agent, AgentManager
from pyclopse.core.session import SessionManager
from pyclopse.core.router import MessageRouter, IncomingMessage, OutgoingMessage
from pyclopse.core.queue import QueueManager


_TOKEN_LOOK_AHEAD = 20  # chars to scan for a delivery token regardless of leading markdown


def _parse_job_token(response: str) -> tuple[str, str]:
    """Detect a delivery token near the start of a job or subagent response.

    Tokens are written by agents at the start of their response to signal how
    the result should reach the user.  All delivery paths (``report_to_agent``
    for scheduled jobs and ``report_to_session`` for subagents) use this same
    function so behaviour is identical regardless of origin.

    Token semantics
    ---------------
    ``NO_REPLY``
        The agent wants to be silent toward the user but still have the result
        in its history for future context.  Detected when ``NO_REPLY`` appears
        within the first ``_TOKEN_LOOK_AHEAD`` characters of the stripped
        response (case-insensitive).  The look-ahead window tolerates leading
        markdown formatting such as ``**NO_REPLY…`` or `` ```\\nNO_REPLY… ``.
        Typical use: heartbeat / pulse jobs where nothing needs attention.

        Result: injected into session history only; channel receives nothing.

    ``SUMMARIZE``
        The agent produced raw data and wants the receiving agent's LLM to
        summarize it before relaying to the user.  Detected when ``SUMMARIZE``
        appears within the first ``_TOKEN_LOOK_AHEAD`` characters (case-insensitive).
        The content delivered to the LLM is everything after the token keyword.

        Result: content injected into history; ``handle_message`` called so the
        agent LLM reads, summarizes, and sends its own reply to the channel.

    *(no token — default)*
        Verbatim delivery.  The raw response is injected into history AND sent
        directly to the user's channel without an additional LLM round-trip.
        Use when output is already user-ready (e.g. formatted scan results).

        Result: response injected into history; raw text posted to channel.

    Returns ``(token, content)`` where *token* is one of ``'NO_REPLY'``,
    ``'SUMMARIZE'``, or ``''`` (empty string = verbatim).  *content* is the
    payload with the token prefix stripped, or the full response for NO_REPLY
    and the verbatim case.

    Args:
        response (str): Raw response string from the agent job or subagent run.

    Returns:
        tuple[str, str]: ``(token, content)`` delivery directive and payload.
    """
    stripped = response.strip()
    head = stripped[:_TOKEN_LOOK_AHEAD].upper()

    if "NO_REPLY" in head:
        return "NO_REPLY", stripped

    summarize_pos = head.find("SUMMARIZE")
    if summarize_pos != -1:
        content = stripped[summarize_pos + len("SUMMARIZE"):].strip()
        return "SUMMARIZE", content

    return "", response


def _build_job_tool_turns(
    job_name: str,
    result: str,
) -> list:
    """Return turns injecting a job result into session history.

    Job results are injected as a user/assistant pair:
    - user:      "[Context: <job> @ HH:MM]"   — signals this is external context
    - assistant: the job result text           — agent's acknowledgement

    This two-message form preserves strict user/assistant alternation required
    by OpenAI-compat providers (MiniMax, etc.) regardless of what the previous
    message in history was.  A single assistant-only turn was previously used but
    caused "tool call result does not follow tool call" (MiniMax 400) whenever
    multiple jobs fired in succession, creating consecutive assistant messages.
    """
    from pyclopse.utils.time import now
    ts = now().strftime("%H:%M")
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": f"[Context: {job_name} @ {ts}]"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": result}],
            "stop_reason": "endTurn",
        },
    ]


async def _inject_turns_to_disk(
    history_path: Any,
    turns: list,
    job_name: str,
    logger: Any,
) -> None:
    """Append *turns* directly to a history.json file without a live runner.

    Uses the same ``{"messages": [...]}`` envelope that FastAgent's
    ``save_messages`` produces so the file remains loadable by ``load_messages``.
    Performs an atomic write with the same current/previous rotation used by
    ``AgentRunner._save_history``.
    """
    import json
    import os
    import tempfile
    from pathlib import Path

    path = Path(history_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        messages: list = []
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            messages = data.get("messages", [])
        messages.extend(turns)
        prev_path = path.parent / "history_previous.json"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=path.parent,
                prefix=".history.tmp.",
                suffix=".json",
            ) as tmp:
                json.dump({"messages": messages}, tmp)
                tmp_path = tmp.name
            if path.exists():
                os.replace(path, prev_path)
            os.replace(tmp_path, path)
            logger.info(
                f"Job {job_name}: injected {len(turns)} turns to disk history (no live runner)"
            )
        except Exception:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
    except Exception as e:
        logger.warning(f"Job {job_name}: disk history injection failed: {e}")


def _snapshot_ctx_tokens(agent: Any, session: Any) -> None:
    """Read token count from the live FA runner and persist it in session.context.

    Called after every agent response so that /status can report context usage
    even when the runner is not currently active (e.g. after a gateway restart
    or before the first message in a new day's session).
    """
    try:
        runner = agent._session_runners.get(session.id)
        if runner is None or getattr(runner, "_app", None) is None:
            return
        agent_name = getattr(runner, "agent_name", None)
        fa_agent = runner._app._agent(agent_name)
        # Prefer exact usage accumulator (Anthropic/OpenAI providers)
        accumulator = getattr(fa_agent, "usage_accumulator", None)
        ctx_tokens = 0
        if accumulator is not None:
            ctx_tokens = accumulator.current_context_tokens or 0
        # Fallback: estimate from live in-memory message history (generic/MiniMax)
        if ctx_tokens == 0:
            history = getattr(fa_agent, "message_history", None) or []
            total_chars = 0
            for msg in history:
                content = getattr(msg, "content", None)
                if content is None:
                    continue
                if isinstance(content, str):
                    total_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        total_chars += len(str(getattr(part, "text", part) or ""))
            ctx_tokens = total_chars // 4
        if ctx_tokens:
            session.context["_ctx_tokens"] = ctx_tokens
    except Exception:
        pass  # Never break message handling over a metrics snapshot


class _GatewayHandleImpl:
    """Concrete :class:`GatewayHandle` backed by a live :class:`Gateway`.

    Exposes a narrow, safe interface to channel plugins while keeping all
    gateway internals private.  Instantiated once via
    :meth:`Gateway._build_gateway_handle` and shared across all plugins.
    """

    def __init__(self, gateway: "Gateway") -> None:
        self._gw = gateway

    # -- Core dispatch --------------------------------------------------------

    async def dispatch(
        self,
        channel: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        on_chunk=None,
    ) -> Optional[str]:
        return await self._gw.handle_message(
            channel=channel,
            sender=user_name,
            sender_id=user_id,
            content=text,
            message_id=message_id,
            agent_id=agent_id,
            on_chunk=on_chunk,
        )

    async def dispatch_command(
        self,
        channel: str,
        user_id: str,
        text: str,
        thread_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[str]:
        if not text.strip().startswith("/"):
            return None
        resolved = self.resolve_agent_id(agent_id)
        session = await self._gw._get_active_session(
            agent_id=resolved, channel=channel, user_id=user_id,
        )
        cmd_name = text.strip().split()[0][1:].lower()
        await self._gw._fire(f"command:{cmd_name}", {
            "command": cmd_name,
            "args": text.strip().split()[1:],
            "session_id": session.id if session else None,
            "channel": channel,
            "sender_id": user_id,
        })
        from pyclopse.core.commands import CommandContext
        ctx = CommandContext(
            gateway=self._gw,
            session=session,
            sender_id=user_id,
            channel=channel,
            thread_id=thread_id,
        )
        return await self._gw._command_registry.dispatch(text.strip(), ctx)

    # -- Helpers --------------------------------------------------------------

    def is_duplicate(self, channel: str, message_id: str) -> bool:
        return self._gw._is_duplicate_message(channel, message_id)

    def resolve_agent_id(self, hint: Optional[str] = None) -> str:
        if hint and self._gw._agent_manager and hint in self._gw._agent_manager.agents:
            return hint
        if self._gw._agent_manager and self._gw._agent_manager.agents:
            return next(iter(self._gw._agent_manager.agents))
        return "default"

    def check_access(
        self,
        user_id: int,
        allowed_users: List[int],
        denied_users: List[int],
    ) -> bool:
        global_denied = getattr(self._gw.config.security, "denied_users", []) or []
        if user_id in global_denied:
            return False
        if denied_users and user_id in denied_users:
            return False
        global_allowed = getattr(self._gw.config.security, "allowed_users", []) or []
        effective_allowed = allowed_users if allowed_users else global_allowed
        if effective_allowed and user_id not in effective_allowed:
            return False
        return True

    def register_endpoint(
        self,
        agent_id: str,
        channel: str,
        endpoint: Dict[str, Any],
    ) -> None:
        ep = self._gw._known_endpoints.setdefault(agent_id, {}).setdefault(channel, {})
        ep.update(endpoint)

    def get_agent_config(self, agent_id: str) -> Optional[Any]:
        """Return the live AgentConfig for *agent_id*, or None."""
        if self._gw._agent_manager:
            agent = self._gw._agent_manager.get_agent(agent_id)
            if agent:
                return getattr(agent, "config", None)
        return None

    def split_message(self, text: str, limit: int = 4096) -> List[str]:
        return self._gw._split_message(text, limit)

    @property
    def config(self) -> Any:
        return self._gw.config


@reflect_system("gateway")
class Gateway:
    """Main orchestrator that wires all pyclopse subsystems together.

    The Gateway is the central coordinator.  It owns all subsystem instances,
    starts/stops the two HTTP servers (MCP on 8081 and REST API on 8080), manages
    Telegram and Slack polling tasks, and routes inbound messages through the full
    pipeline: deduplication → allowlist/denylist → session → command → queue →
    agent → channel reply.

    Typical lifecycle::

        gw = Gateway(config_path)
        await gw.initialize()
        await gw.start()  # blocks until Ctrl+C
        await gw.stop()

    The ``handle_message()`` method is the single entry point for all inbound
    messages regardless of origin channel.  It returns the agent reply string.
    Job execution for agent-type jobs flows through ``_agent_executor()``
    which always strips thinking tags before delivering results.

    **Cross-channel sync** — when ``agent.config.channel_sync`` is True (the
    default), every inbound message and agent response is mirrored to all other
    channels that have interacted with the same agent session.  Messages appear
    natively in each channel's history with no source prefix.  The sync system
    has two delivery paths:

    - *Event bus* (``_publish``) — asyncio queues consumed by the TUI every
      0.3 s via ``_drain_events``.  Three event types: ``user_message``,
      ``agent_response``, and ``stream_chunk`` (incremental LLM output).
    - *Direct API fan-out* — ``_fan_out_user_message`` (fire-and-forget) and
      ``_fan_out_response`` send to Telegram/Slack via their bot APIs.
      Thinking content is formatted as expandable blockquotes for Telegram and
      stripped for Slack.

    The TUI is a first-class channel: it calls ``handle_message(channel="tui")``
    without an ``on_chunk`` callback and renders streaming output from
    ``stream_chunk`` events on its Textual main thread.
    """

    def __init__(self, config_path: Optional[str] = None):
        # Configuration
        self._config_loader = ConfigLoader(config_path)
        self._config: Optional[Config] = None

        # Subsystems
        self._audit_logger: Optional[AuditLogger] = None
        self._approval_system: Optional[ExecApprovalSystem] = None
        self._sandbox: Optional[Sandbox] = None
        self._job_scheduler: Optional[JobScheduler] = None
        self._todo_store: Optional[Any] = None
        self._file_watcher: Optional[Any] = None

        # Core
        self._agent_manager: Optional[AgentManager] = None
        self._session_manager: Optional[SessionManager] = None
        self._router: Optional[MessageRouter] = None

        # Runtime
        self._is_running = False
        self._initialized = False
        self._startup_tasks: List[asyncio.Task] = []
        self._logger = logging.getLogger("pyclopse.gateway")
        # Cross-channel event bus: agent_id → subscriber queues
        self._agent_listeners: Dict[str, List[asyncio.Queue]] = {}
        # Gateway-level endpoint cache: agent_id → channel → {sender_id, sender}
        # Updated on every inbound message so fan-out works even before session
        # context has accumulated endpoints (e.g. first TUI message before Telegram).
        self._known_endpoints: Dict[str, Dict[str, dict]] = {}

        # Channel adapters (to be implemented)
        self._channels: Dict[str, Any] = {}

        # Hook system
        self._hook_registry: Optional[Any] = None   # HookRegistry
        self._memory_service: Optional[Any] = None  # MemoryService

        # Tracks session IDs we've already seen (for session:created detection)
        self._known_session_ids: set = set()

        # Command registry
        from pyclopse.core.commands import CommandRegistry, register_builtin_commands
        self._command_registry = CommandRegistry()
        register_builtin_commands(self._command_registry, self)

        # Legacy Telegram state — kept for backward compat with code that
        # references these dicts (fan-out legacy branches).  Will be removed
        # once all channels are migrated to plugins.
        self._tg_bots: Dict[str, Any] = {}
        self._tg_chat_ids: Dict[str, Optional[str]] = {}
        self._tg_polling_tasks: Dict[str, asyncio.Task] = {}
        self._slack_web_client: Optional[Any] = None  # AsyncWebClient for outbound Slack messages
        # Active processing tasks keyed by session ID (used by /stop)
        self._active_tasks: Dict[str, asyncio.Task] = {}

        # Per-session message queue (handles rapid inbound bursts)
        self._queue_manager = QueueManager()

        # Inbound dedup cache: "channel:message_id" → timestamp of first processing
        self._seen_message_ids: Dict[str, float] = {}
        self._dedup_ttl_seconds: int = 60

        # MCP server task (managed by start_mcp_server / stop_mcp_server)
        self._mcp_server_task: Optional[asyncio.Task] = None
        # REST API server task (managed by start_api_server / stop_api_server)
        self._api_server_task: Optional[asyncio.Task] = None
        self._api_uvicorn_server: Optional[Any] = None

        # Usage counters
        import time as _time
        self._usage: Dict[str, Any] = {
            "messages_total": 0,
            "messages_by_agent": {},
            "messages_by_channel": {},
            "started_at": _time.time(),
        }
        # Thread/topic → agent bindings set by /focus
        self._thread_bindings: Dict[str, str] = {}

    @property
    def config(self) -> Config:
        """Get configuration."""
        if self._config is None:
            self._config = self._config_loader.load()
        return self._config

    @property
    def agent_manager(self) -> AgentManager:
        """Get agent manager."""
        if self._agent_manager is None:
            self._agent_manager = AgentManager()
        return self._agent_manager

    @property
    def session_manager(self) -> SessionManager:
        """Get session manager."""
        if self._session_manager is None:
            sc = self._config.sessions if self._config is not None else None

            async def _on_session_expire(session: Any) -> None:
                # Evict the in-memory AgentRunner so its FastAgent MCP connection
                # is closed and memory is freed.  Without this, runners accumulate
                # in agent._session_runners indefinitely as sessions expire via TTL,
                # slowly exhausting MCP server connections over time.
                if self._agent_manager:
                    for agent in self._agent_manager.agents.values():
                        await agent.evict_session_runner(session.id)
                await self._fire(HookEvent.SESSION_EXPIRED, {
                    "session_id": session.id,
                    "agent_id": session.agent_id,
                    "channel": session.channel,
                    "user_id": session.user_id,
                })

            async def _on_session_rollover(session_id: str) -> None:
                if self._agent_manager:
                    for agent in self._agent_manager.agents.values():
                        await agent.evict_session_runner(session_id)

            self._session_manager = SessionManager(
                persist_dir=sc.persist_dir if sc else "~/.pyclopse/sessions",
                ttl_hours=sc.ttl_hours if sc else 24,
                reaper_interval_minutes=sc.reaper_interval_minutes if sc else 60,
                on_expire=_on_session_expire,
                daily_rollover=sc.daily_rollover if sc else True,
                on_rollover=_on_session_rollover,
            )
        return self._session_manager

    @property
    def router(self) -> MessageRouter:
        """Get message router."""
        if self._router is None:
            self._router = MessageRouter(self.config)
        return self._router

    @property
    def audit_logger(self) -> Optional[AuditLogger]:
        """Get audit logger."""
        return self._audit_logger

    @property
    def approval_system(self) -> Optional[ExecApprovalSystem]:
        """Get approval system."""
        return self._approval_system

    @property
    def sandbox(self) -> Optional[Sandbox]:
        """Get sandbox."""
        return self._sandbox

    @property
    def job_scheduler(self) -> Optional[JobScheduler]:
        """Get job scheduler."""
        return self._job_scheduler

    @property
    def hook_registry(self) -> Optional[Any]:
        """Get the global HookRegistry (available after initialize())."""
        return self._hook_registry

    @property
    def memory_service(self) -> Optional[Any]:
        """Get the global MemoryService (available after initialize())."""
        return self._memory_service

    # ── backward-compat accessors for single-bot code paths ──────────────────

    def _ensure_tg_dicts(self) -> None:
        """Lazily initialise multi-bot dicts (called by compat properties/setters
        so they work on Gateway instances created via __new__ in unit tests)."""
        if not hasattr(self, "_tg_bots"):
            self._tg_bots = {}
        if not hasattr(self, "_tg_chat_ids"):
            self._tg_chat_ids = {}
        if not hasattr(self, "_tg_polling_tasks"):
            self._tg_polling_tasks = {}

    @property
    def _telegram_bot(self) -> Optional[Any]:
        """Return the first configured Telegram bot (compat with single-bot code)."""
        self._ensure_tg_dicts()
        return next(iter(self._tg_bots.values()), None)

    @_telegram_bot.setter
    def _telegram_bot(self, v: Optional[Any]) -> None:
        """Set/clear the single-bot entry under key '_default' (compat for tests)."""
        self._ensure_tg_dicts()
        if v is None:
            self._tg_bots.pop("_default", None)
        else:
            self._tg_bots["_default"] = v
            self._tg_chat_ids.setdefault("_default", None)

    @property
    def _telegram_chat_id(self) -> Optional[str]:
        """Return the first bot's chat_id (compat with single-bot code)."""
        self._ensure_tg_dicts()
        return next(iter(self._tg_chat_ids.values()), None)

    @_telegram_chat_id.setter
    def _telegram_chat_id(self, v: Optional[str]) -> None:
        """Set the first bot's chat_id; creates '_default' entry when dict is empty."""
        self._ensure_tg_dicts()
        if self._tg_chat_ids:
            k = next(iter(self._tg_chat_ids))
            self._tg_chat_ids[k] = v
        else:
            self._tg_chat_ids["_default"] = v

    @property
    def _telegram_polling_task(self) -> Optional[asyncio.Task]:
        """Return the first polling task (compat)."""
        self._ensure_tg_dicts()
        return next(iter(self._tg_polling_tasks.values()), None)

    @_telegram_polling_task.setter
    def _telegram_polling_task(self, v: Optional[asyncio.Task]) -> None:
        """Set/clear the single polling task under '_default' (compat for tests)."""
        self._ensure_tg_dicts()
        if v is None:
            self._tg_polling_tasks.pop("_default", None)
        else:
            self._tg_polling_tasks["_default"] = v

    @property
    def telegram_bot(self) -> Optional[Any]:
        """Get Telegram bot instance (first bot in multi-bot setups)."""
        return self._telegram_bot

    def set_telegram_target(self, chat_id: str, bot_name: Optional[str] = None) -> None:
        """Set the Telegram chat ID for pulse messages.

        In multi-bot setups pass ``bot_name`` to target a specific bot.
        Defaults to the first bot when omitted.
        """
        self._ensure_tg_dicts()
        if bot_name and bot_name in self._tg_chat_ids:
            self._tg_chat_ids[bot_name] = chat_id
        elif self._tg_chat_ids:
            k = next(iter(self._tg_chat_ids))
            self._tg_chat_ids[k] = chat_id

    async def initialize(self) -> None:
        """Initialize all subsystems.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._initialized:
            self._logger.debug("Gateway already initialized, skipping")
            return

        self._logger.info("Initializing pyclopse Gateway...")

        # Load config
        self._config = self._config_loader.load()
        self._logger.info(f"Loaded config (version: {self.config.version})")

        # Initialize hook registry first (other subsystems can register hooks)
        await self._init_hooks()

        # Initialize memory service (depends on hook registry)
        await self._init_memory_service()

        # Initialize security
        await self._init_security()

        # Initialize concurrency limits
        await self._init_concurrency()

        # Bootstrap OTel span store before agents init so FastAgent's lazy
        # trace.get_tracer() calls are backed by our in-process provider.
        from pyclopse.core import otel_store as _otel_store_mod
        self._otel_store = _otel_store_mod.bootstrap()

        # Initialize core
        await self._init_core()

        # Mount A2A endpoints (after core so agents are ready, before channels)
        await self._init_a2a()

        # Initialize channels
        await self._init_channels()

        # Initialize jobs
        await self._init_jobs()

        # Initialize TODO store
        await self._init_todos()

        # Start file watcher (config + jobs hot-reload)
        await self._init_file_watcher()

        self._initialized = True
        self._logger.info("Gateway initialization complete")

        # Fire startup hook (all subsystems are ready)
        await self._fire(HookEvent.GATEWAY_STARTUP, {
            "config_version": self.config.version,
            "agents": list(self.agent_manager.agents.keys()),
        })

    async def _fire(self, event: str, context: Dict[str, Any]) -> None:
        """Fire a notification hook if the registry is available."""
        if self._hook_registry is not None:
            await self._hook_registry.notify(event, context)

    async def _init_hooks(self) -> None:
        """Create the HookRegistry and load all configured hooks."""
        from pyclopse.hooks.registry import HookRegistry
        from pyclopse.hooks.loader import HookLoader

        self._hook_registry = HookRegistry()

        # Determine config_dir from the config file path
        config_dir = "~/.pyclopse"
        if self._config_loader.config_path:
            p = self._config_loader.config_path
            config_dir = str(p.parent if p.is_file() else p)

        hooks_cfg = self.config.hooks
        if not (hooks_cfg.internal or hooks_cfg.external):
            self._logger.info("Hooks disabled (internal=False, external=False)")
            return

        loader = HookLoader(config_dir=config_dir)

        # Determine which hooks are explicitly enabled via config entries
        # If entries dict is empty, allow all; otherwise only those set to enabled=True
        enabled_names: Optional[List[str]] = None
        if hooks_cfg.entries:
            enabled_names = [
                name for name, entry in hooks_cfg.entries.items() if entry.enabled
            ]

        count = loader.register_all(self._hook_registry, enabled_names=enabled_names)
        self._logger.info(f"Hook registry ready ({count} hooks loaded)")

    async def _init_memory_service(self) -> None:
        """Instantiate MemoryService and set as the global singleton."""
        from pyclopse.memory.service import MemoryService, set_memory_service
        from pyclopse.memory.embeddings import make_embedding_backend

        mem_cfg = self.config.memory

        # Build optional embedding backend
        embedding_backend = None
        try:
            embedding_backend = make_embedding_backend(mem_cfg.embedding)
            if embedding_backend:
                self._logger.info(
                    f"Embedding backend: {mem_cfg.embedding.provider} "
                    f"model={mem_cfg.embedding.model or '(default)'}"
                )
        except Exception as exc:
            self._logger.warning(f"Could not init embedding backend: {exc}")

        from pyclopse.memory.file_backend import FileMemoryBackend
        config_dir = "~/.pyclopse"
        if self._config_loader.config_path:
            p = self._config_loader.config_path
            config_dir = str(p.parent if p.is_file() else p)
        default_backend = FileMemoryBackend(
            base_dir=str(Path(config_dir).expanduser() / "agents" / "gateway"),
            embedding_backend=embedding_backend,
        )

        self._memory_service = MemoryService(
            registry=self._hook_registry,
            default_backend=default_backend,
        )
        set_memory_service(self._memory_service)
        self._logger.info(f"Memory service ready (backend={mem_cfg.backend})")

    @staticmethod
    def _collect_model_limits(providers_cfg: Any) -> Dict[str, int]:
        """Collect per-model concurrency limits from all configured providers."""
        limits: Dict[str, int] = {}
        all_providers = list(vars(providers_cfg).values()) + list(
            (getattr(providers_cfg, "model_extra", None) or {}).values()
        )
        for provider in all_providers:
            if provider is None:
                continue
            models = getattr(provider, "models", None)
            if not models:
                continue
            for model_name, model_cfg in models.items():
                if getattr(model_cfg, "enabled", True):
                    limits[model_name] = model_cfg.concurrency
        return limits

    async def _init_concurrency(self) -> None:
        """Initialize per-model concurrency manager and usage monitors from config."""
        from pyclopse.core.concurrency import init_manager
        from pyclopse.core.usage import init_registry
        cc = self.config.concurrency
        model_limits = self._collect_model_limits(self.config.providers)
        init_manager(model_limits=model_limits, default=cc.default)
        self._logger.info(
            f"Concurrency manager: default={cc.default}, models={model_limits or '{}'}"
        )
        # Start usage monitors for providers that have usage: configured
        registry = init_registry(self.config.providers)
        await registry.start_all()

    async def _init_security(self) -> None:
        """Initialize security subsystem."""
        security_config: SecurityConfig = self.config.security

        # Audit logger
        if security_config.audit.enabled:
            self._audit_logger = AuditLogger(
                log_file=security_config.audit.log_file,
                retention_days=security_config.audit.retention_days,
            )
            self._logger.info("Audit logger initialized")

        # Exec approvals
        self._approval_system = ExecApprovalSystem(
            security_config.exec_approvals,
        )
        self._logger.info(
            f"Approval system initialized (mode: {security_config.exec_approvals.mode.value})"
        )

        # Sandbox
        self._sandbox = create_sandbox(security_config.sandbox)
        self._logger.info(f"Sandbox initialized (type: {security_config.sandbox.type})")

    async def _init_core(self) -> None:
        """Initialize core subsystems."""
        # Configure timezone first — all subsequent timestamps use this zone
        from pyclopse.utils.time import configure_timezone
        configure_timezone(getattr(self._config, "timezone", None) if self._config else None)

        # Session manager
        await self.session_manager.start()
        self._logger.info("Session manager started")

        # Create default agent from config
        for agent_id, agent_config_dict in self.config.agents.model_dump().items():
            name = agent_config_dict.get("name", agent_id)
            # Extract provider config if present (per-agent provider: block)
            provider_config = agent_config_dict.get("provider")
            # If the agent model uses "provider/model" syntax, look up the named provider
            # and merge its config so credentials flow through to the agent runner.
            model_str = str(agent_config_dict.get("model", ""))
            if "/" in model_str:
                prov_name = model_str.split("/", 1)[0]
                prov_cfg = getattr(self.config.providers, prov_name, None)
                if prov_cfg and getattr(prov_cfg, "fastagent_provider", None):
                    prov_dict = prov_cfg.model_dump()
                    if provider_config:
                        merged = {**prov_dict, **provider_config}
                    else:
                        merged = {**prov_dict, "type": prov_name}
                    provider_config = merged
            # Convert dict to AgentConfig object
            agent_config = AgentConfig(**agent_config_dict)

            # Get config_dir from config loader (default: ~/.pyclopse)
            # config_path could be a file like ~/.pyclopse/config/pyclopse.yaml, so get parent
            if self._config_loader.config_path:
                config_path_obj = self._config_loader.config_path
                if config_path_obj.is_file():
                    config_dir = str(config_path_obj.parent)
                else:
                    config_dir = str(config_path_obj)
            else:
                config_dir = "~/.pyclopse"

            self.agent_manager.create_agent(
                agent_id=agent_id,
                name=name,
                config=agent_config,
                provider_config=provider_config,
                session_manager=self.session_manager,
                config_dir=config_dir,
                pyclopse_config=self._config,
            )

        await self.agent_manager.start_all()
        self._logger.info(f"Started {len(self.agent_manager.agents)} agents")

    # -- Gateway handle for channel plugins ------------------------------------

    def _build_gateway_handle(self) -> "GatewayHandle":
        """Create (or return cached) :class:`_GatewayHandleImpl` for plugins."""
        if not hasattr(self, "_gw_handle") or self._gw_handle is None:
            self._gw_handle = _GatewayHandleImpl(self)
        return self._gw_handle

    async def _init_channel_plugins(self) -> None:
        """Load, start, and register all channel plugins."""
        from pyclopse.channels.loader import load_all

        specs = list(self.config.plugins.channels)
        handle = self._build_gateway_handle()

        plugins = load_all(specs)
        for plugin in plugins:
            name = getattr(plugin, "name", type(plugin).__name__)
            try:
                await plugin.start(handle)
                self._channels[name] = plugin
                self._logger.info(f"Channel plugin '{name}' started")
            except Exception as exc:
                self._logger.error(
                    f"Channel plugin '{name}' failed to start: {exc}"
                )

        if plugins:
            self._logger.info(
                f"{len(self._channels)} channel plugin(s) active: "
                f"{list(self._channels)}"
            )

    async def _init_channels(self) -> None:
        """Initialize channel adapters."""
        handle = self._build_gateway_handle()

        # Register built-in channels based on config
        await self._init_builtin_channels(handle)

        # Load and start external channel plugins
        await self._init_channel_plugins()

        # Initialize Slack outbound client if configured (legacy — will become plugin)
        slack_config = self.config.channels.slack if self.config.channels else None
        if slack_config and slack_config.enabled and slack_config.bot_token:
            try:
                from slack_sdk.web.async_client import AsyncWebClient
                self._slack_web_client = AsyncWebClient(token=slack_config.bot_token)
                self._logger.info("Slack outbound client initialized")
            except ImportError:
                self._logger.warning("slack-sdk not installed, Slack outbound disabled")
            except Exception as e:
                self._logger.error(f"Failed to initialize Slack client: {e}")

    @staticmethod
    def _builtin_plugin_classes() -> List[tuple]:
        """Return (channel_name, plugin_class) for each built-in channel.

        Uses lazy imports so missing optional dependencies (e.g. discord.py)
        don't prevent other channels from loading.
        """
        result = []
        try:
            from pyclopse.channels.telegram_plugin import TelegramPlugin
            result.append(("telegram", TelegramPlugin))
        except ImportError:
            pass
        try:
            from pyclopse.channels.discord_plugin import DiscordPlugin
            result.append(("discord", DiscordPlugin))
        except ImportError:
            pass
        try:
            from pyclopse.channels.whatsapp_plugin import WhatsAppPlugin
            result.append(("whatsapp", WhatsAppPlugin))
        except ImportError:
            pass
        return result

    async def _init_builtin_channels(self, handle: Any) -> None:
        """Register built-in channel plugins based on config.

        For each built-in plugin, checks whether the channel is configured
        (i.e. ``channels.{name}`` exists in YAML), validates the config
        against the plugin's declared schema, and starts the plugin if enabled.
        """
        channels_cfg = self.config.channels
        if not channels_cfg:
            return

        for channel_name, plugin_cls in self._builtin_plugin_classes():
            # Read raw config for this channel (could be a dict via extra="allow")
            raw = getattr(channels_cfg, channel_name, None)
            if raw is None:
                continue
            # Validate against the plugin's declared schema
            try:
                cfg = plugin_cls.config_schema.model_validate(
                    raw if isinstance(raw, dict) else (
                        raw.model_dump(by_alias=True) if hasattr(raw, "model_dump") else dict(raw)
                    )
                )
            except Exception as e:
                self._logger.error(f"Invalid config for '{channel_name}': {e}")
                continue
            if not cfg.enabled:
                self._logger.debug(f"Channel '{channel_name}' disabled in config")
                continue
            try:
                plugin = plugin_cls()
                await plugin.start(handle)
                self._channels[channel_name] = plugin
                self._logger.info(f"{channel_name} channel plugin started")
            except Exception as e:
                self._logger.error(f"{channel_name} plugin failed to start: {e}")

    # Legacy Telegram methods removed — now handled by TelegramPlugin

    async def _init_jobs(self) -> None:
        """Initialize job scheduler."""
        from pyclopse.jobs.models import JobStatus, DeliverAnnounce

        async def _agent_executor(job: Any) -> dict:
            """Run an agent-type job: send message to agent, return response."""
            import uuid as _uuid
            from pyclopse.core.prompt_builder import build_job_prompt

            agent_run = job.run
            job_agent_id = getattr(agent_run, "agent", None)
            deliver = getattr(job, "deliver", None)

            # Session key: isolated = unique per run, persistent = shared per job
            session_mode = getattr(agent_run, "session_mode", "isolated")
            if session_mode == "isolated":
                session_user_id = f"job-{job.id}-{_uuid.uuid4().hex[:8]}"
            else:
                session_user_id = f"job-{job.id}"

            # Derive config_dir (same logic as _init_agents)
            if self._config_loader.config_path:
                _cp = self._config_loader.config_path
                _config_dir = str(_cp.parent if _cp.is_file() else _cp)
            else:
                _config_dir = "~/.pyclopse"

            # Build the job-specific system prompt from AgentRun flags.
            # Combine gateway.skills_dirs (global) + agent.skills_dirs (per-agent).
            _gw_skill_dirs = list(self.config.gateway.skills_dirs or [])
            _agent_obj = self.agent_manager.agents.get(job_agent_id or "")
            _agent_skill_dirs = list(getattr(getattr(_agent_obj, "config", None), "skills_dirs", None) or [])
            _extra_dirs = _gw_skill_dirs + _agent_skill_dirs or None
            job_instruction = build_job_prompt(
                agent_name=job_agent_id or "",
                config_dir=_config_dir,
                agent_run=agent_run,
                extra_dirs=_extra_dirs,
            )

            is_subagent = getattr(job, "spawned_by_session", None) is not None
            session = None
            try:
                # Create/get session with the right key.
                # Isolated sessions are ephemeral — no session.json written to disk so
                # they don't accumulate in ~/.pyclopse/agents/{agent}/sessions/.
                session = await self._get_or_create_session(
                    agent_id=job_agent_id,
                    channel="job",
                    user_id=session_user_id,
                    ephemeral=(session_mode == "isolated"),
                )
                if session is None:
                    return {"success": False, "error": "Could not create job session"}

                # Inject job prompt, priority and ephemeral flag into session context
                session.context["instruction_override"] = job_instruction
                session.context["_priority"] = getattr(job, "priority", "normal")
                if session_mode == "isolated":
                    session.context["no_history"] = True

                # Register session ID in scheduler so subagent tools can find it
                if is_subagent and self._job_scheduler:
                    self._job_scheduler._subagent_sessions[job.id] = session.id

                response = await self.handle_message(
                    content=agent_run.message,
                    agent_id=job_agent_id,
                    sender="job-scheduler",
                    sender_id=session_user_id,
                    channel="job",
                )

                # Process any queued follow-up messages (subagent_send)
                if is_subagent and self._job_scheduler:
                    queued = self._job_scheduler.pop_queued_messages(job.id)
                    for follow_up in queued:
                        try:
                            response = await self.handle_message(
                                content=follow_up,
                                agent_id=job_agent_id,
                                sender="job-scheduler",
                                sender_id=session_user_id,
                                channel="job",
                            )
                            # Deliver each follow-up response back to spawning session
                            await self._deliver_to_spawning_session(job, agent_run, response, prefix="↩️")
                        except Exception as _qe:
                            self._logger.error(f"subagent follow-up failed: {_qe}")

                # Always strip thinking tags from job results before delivery
                if response:
                    from pyclopse.agents.runner import strip_thinking_tags
                    response = strip_thinking_tags(response)

                # Deliver result: report_to_session takes priority over report_to_agent
                report_session_id = getattr(agent_run, "report_to_session", None)
                report_agent = getattr(agent_run, "report_to_agent", None)
                self._logger.info(
                    f"Job {job.name}: report_to_session={report_session_id!r} "
                    f"report_to_agent={report_agent!r} response_len={len(response) if response else 0}"
                )
                if report_session_id and response:
                    await self._deliver_to_spawning_session(job, agent_run, response)
                elif report_agent and response:
                    try:
                        target_session = await self.session_manager.get_active_session(report_agent)
                        self._logger.info(
                            f"Job {job.name}: report_to_agent={report_agent!r} "
                            f"target_session={target_session.id if target_session else None}"
                        )
                        if target_session:
                            await self._deliver_result(job.name, report_agent, target_session, response)
                        else:
                            self._logger.warning(
                                f"Job {job.name}: no active session for {report_agent!r}, result dropped"
                            )
                    except Exception as _re:
                        self._logger.error(f"report_to_agent delivery failed: {_re}", exc_info=True)
                elif report_agent and not response:
                    # Empty response — treat as implicit NO_REPLY and drop silently.
                    # Escalating to handle_message would cause the agent to generate
                    # an unnecessary reply to the user about a job that had nothing to say.
                    self._logger.warning(
                        f"Job {job.name}: report_to_agent={report_agent!r} but response is empty — dropped"
                    )

                return {
                    "success": True,
                    "stdout": response or "",
                    "stderr": "",
                    "exit_code": 0,
                }
            except Exception as e:
                return {"success": False, "error": str(e), "exit_code": 1}
            finally:
                # Evict isolated session runners and session index entries immediately
                # to free memory — ephemeral sessions have no disk representation so
                # there is no value in keeping them in memory after the run completes.
                if session_mode == "isolated" and session is not None:
                    if self._agent_manager:
                        agent_obj = self._agent_manager.get_agent(job_agent_id)
                        if agent_obj:
                            await agent_obj.evict_session_runner(session.id)
                    if self._session_manager:
                        self._session_manager._remove_from_index(session.id)

        async def _job_notify(job: Any, run: Any) -> None:
            """Send job completion/failure notification."""
            spawning_session_id = getattr(job, "spawned_by_session", None)

            # Subagent jobs: send start/finish directly to the spawning session's
            # channel without going through the agent LLM (no _run_lock contention).
            if spawning_session_id is not None:
                if not self._session_manager:
                    return
                try:
                    spawning_session = await self._session_manager.get_session(spawning_session_id)
                except Exception:
                    spawning_session = None
                if not spawning_session:
                    return
                label = getattr(job, "name", job.id)
                if run.status == JobStatus.RUNNING:
                    text = f"▶️ Subagent *{label}* started."
                else:
                    ok = run.status == JobStatus.COMPLETED
                    icon = "✅" if ok else "❌"
                    duration = f" ({run.duration_ms():.0f}ms)" if run.duration_ms() else ""
                    text = f"{icon} Subagent *{label}* finished{duration}."
                    if run.error:
                        text += f"\nError: {run.error}"
                try:
                    await self._deliver_to_channel(spawning_session, text)
                except Exception as e:
                    self._logger.error(f"Subagent notify send failed: {e}")
                return

            # Determine delivery target
            deliver = getattr(job, "deliver", None)
            if deliver and getattr(deliver, "mode", None) == "none":
                return

            # Webhook delivery (completion only)
            if deliver and getattr(deliver, "mode", None) == "webhook":
                if run.status == JobStatus.RUNNING:
                    return
                try:
                    import httpx
                    payload = {
                        "job_id": job.id,
                        "job_name": job.name,
                        "status": run.status.value,
                        "stdout": run.stdout,
                        "error": run.error,
                        "duration_ms": run.duration_ms(),
                    }
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(deliver.url, json=payload)
                except Exception as e:
                    self._logger.error(f"Webhook delivery failed: {e}")
                return

            is_timeout = (
                run.status == JobStatus.FAILED
                and bool(run.error and "timed out" in run.error.lower())
            )

            if run.status == JobStatus.RUNNING:
                text = f"▶️ Job *{job.name}* started."
            elif is_timeout:
                duration = f" ({run.duration_ms():.0f}ms)" if run.duration_ms() else ""
                text = f"⏱️ Job *{job.name}* timed out{duration}."
            else:
                ok = run.status == JobStatus.COMPLETED
                icon = "✅" if ok else "❌"
                duration = f" ({run.duration_ms():.0f}ms)" if run.duration_ms() else ""
                text = f"{icon} Job *{job.name}* finished{duration}."
                if run.error:
                    text += f"\nError: {run.error}"

            # Resolve the owning agent: agent-type jobs have job.run.agent,
            # command-type jobs are tracked in the scheduler's _job_agents dict.
            job_agent_id = getattr(getattr(job, "run", None), "agent", None)
            if not job_agent_id and self._job_scheduler:
                job_agent_id = self._job_scheduler._job_agents.get(job.id)
            notify_session = None
            if job_agent_id and self._session_manager:
                try:
                    notify_session = await self._get_active_session(
                        agent_id=job_agent_id, channel="telegram", user_id="",
                    )
                except Exception:
                    pass

            if notify_session:
                try:
                    await self._deliver_to_channel(notify_session, text)
                except Exception as e:
                    self._logger.error(f"Job notify send failed: {e}")
            else:
                # No session — try direct plugin send using known endpoint or
                # deliver.chat_id.  Resolve the correct bot for this agent so
                # the notification appears from the right bot, not the default.
                ep = self._known_endpoints.get(job_agent_id or "", {}).get("telegram", {})
                chat_id = ep.get("sender_id") or (
                    getattr(deliver, "chat_id", None) if deliver else None
                )
                bot_name = ep.get("bot_name")
                # If no bot_name from endpoint, ask the plugin which bot serves this agent
                if not bot_name and job_agent_id and "telegram" in self._channels:
                    tg_plugin = self._channels["telegram"]
                    if hasattr(tg_plugin, "bot_for_agent"):
                        _, bot_name = tg_plugin.bot_for_agent(job_agent_id)
                if chat_id and "telegram" in self._channels:
                    try:
                        from pyclopse.channels.base import MessageTarget
                        target = MessageTarget(channel="telegram", user_id=chat_id)
                        await self._channels["telegram"].send_message(
                            target, text, bot_name=bot_name,
                        )
                    except Exception as e:
                        self._logger.error(f"Job notify send failed: {e}")

            # On timeout, notify report_to_agent so it can tell the user
            if is_timeout and run.status != JobStatus.RUNNING:
                report_agent = getattr(getattr(job, "run", None), "report_to_agent", None)
                if report_agent:
                    try:
                        target_session = await self.session_manager.get_active_session(report_agent)
                        if target_session:
                            _channel = target_session.last_channel or target_session.channel
                            _user_id = target_session.last_user_id or target_session.user_id
                            await self.handle_message(
                                channel=_channel,
                                sender="job-scheduler",
                                sender_id=_user_id,
                                content=(
                                    f"[System] Job '{job.name}' timed out after {job.timeout_seconds}s. "
                                    f"Please let the user know the scan did not complete in time."
                                ),
                                agent_id=report_agent,
                                dispatch_response=True,
                            )
                    except Exception as _te:
                        self._logger.error(f"report_to_agent timeout delivery failed: {_te}", exc_info=True)

        self._job_scheduler = JobScheduler(
            self.config.jobs,
            agent_executor=_agent_executor,
            notify_callback=_job_notify,
            default_timezone=self.config.timezone,
        )
        await self._job_scheduler.start()
        self._logger.info("Job scheduler started")

    async def _init_a2a(self) -> None:
        """Mount A2A (Agent-to-Agent) protocol endpoints for enabled agents."""
        try:
            from pyclopse.a2a.setup import mount_a2a_routes
            from pyclopse.api.app import _app as _fastapi_app  # noqa: F401 — may be None
        except ImportError:
            return

        from pyclopse.api import app as _api_module
        fastapi_app = getattr(_api_module, "_app", None)
        if fastapi_app is None:
            self._logger.debug("A2A: FastAPI app not available yet, skipping")
            return

        try:
            n = mount_a2a_routes(self, fastapi_app)
            if n:
                self._logger.info(f"A2A: {n} agent(s) exposed")
        except Exception as e:
            self._logger.warning(f"A2A init failed: {e}")

    async def _init_todos(self) -> None:
        """Initialize the TODO store."""
        from pyclopse.todos.store import TodoStore
        persist = self.config.todos.persist_file if hasattr(self.config, "todos") else "~/.pyclopse/todos.json"
        self._todo_store = TodoStore(persist_path=persist)
        self._logger.info(f"TODO store initialised ({persist})")

    async def _init_file_watcher(self) -> None:
        """Start a file watcher for config.yaml and all agents/*/jobs.yaml.

        When a watched file changes on disk the relevant subsystem is reloaded
        automatically (config → gateway.reload_config, jobs → scheduler.reload_agent_jobs).
        The scheduler is also given a reference to the watcher so it can
        acknowledge its own writes and avoid spurious reload loops.
        """
        from pyclopse.core.watcher import FileWatcher

        watcher = FileWatcher(poll_interval=0.5, debounce=0.5)
        watched = 0

        # Watch the main config file
        if self._config_loader.config_path:
            config_file = Path(self._config_loader.config_path)
            if config_file.exists():
                async def _on_config_change() -> None:
                    self._logger.info("Config file changed — reloading")
                    try:
                        await self.reload_config()
                        # Acknowledge so watcher doesn't fire again for our own writes
                        watcher.acknowledge(config_file)
                    except Exception as exc:
                        self._logger.warning(f"Config hot-reload failed: {exc}")

                watcher.watch(config_file, _on_config_change)
                watched += 1

        # Watch each agent's jobs.yaml
        if self._job_scheduler and self._agent_manager:
            for agent_id in list(self._agent_manager.agents):
                jobs_path = self._job_scheduler._agents_dir / agent_id / "jobs.yaml"
                if jobs_path.exists():
                    async def _on_jobs_change(aid: str = agent_id) -> None:
                        self._logger.info(f"jobs.yaml changed for '{aid}' — reloading")
                        try:
                            await self._job_scheduler.reload_agent_jobs(aid)
                        except Exception as exc:
                            self._logger.warning(f"Jobs hot-reload failed for '{aid}': {exc}")

                    watcher.watch(jobs_path, _on_jobs_change)
                    watched += 1

            # Give the scheduler a reference so _flush() can acknowledge writes
            self._job_scheduler._file_watcher = watcher

        await watcher.start()
        self._file_watcher = watcher
        self._logger.info(f"File watcher started — watching {watched} file(s)")


    def _is_duplicate_message(self, channel: str, message_id: str) -> bool:
        """Return True if this message was already processed (dedup check).

        Evicts entries older than _dedup_ttl_seconds as a side effect.
        """
        import time as _time
        now = _time.monotonic()
        key = f"{channel}:{message_id}"

        # Evict stale entries
        stale = [k for k, ts in self._seen_message_ids.items() if now - ts > self._dedup_ttl_seconds]
        for k in stale:
            del self._seen_message_ids[k]

        if key in self._seen_message_ids:
            return True
        self._seen_message_ids[key] = now
        return False

    # Legacy Telegram methods (_telegram_poll_bot, _telegram_poll,
    # _handle_telegram_message, _stream_telegram_response) removed.
    # Now handled by TelegramPlugin (pyclopse/channels/telegram_plugin.py).

    async def _handle_slack_message(
        self,
        event: Dict[str, Any],
        slack_client: Any,
    ) -> None:
        """Handle an incoming Slack event dict and send the agent's reply.

        Parameters
        ----------
        event:
            Slack event payload containing at minimum:
              - ``user``       — Slack user ID
              - ``channel``    — Slack channel ID
              - ``text``       — message text
              - ``ts``         — message timestamp (Slack message ID)
              - ``thread_ts``  — (optional) parent thread timestamp
        slack_client:
            An async Slack SDK WebClient (or compatible mock) used to post
            the reply.

        Threading behaviour (controlled by ``config.channels.slack.threading``):
        - When ``threading=True`` and the message has a ``thread_ts`` (i.e. it
          is already inside a thread), the reply is posted to that thread.
        - When ``threading=True`` and the message is a top-level message (no
          ``thread_ts``), the reply starts a new thread using ``ts`` as the
          ``thread_ts``.
        - When ``threading=False``, replies are posted to the channel without
          a thread timestamp.

        Session keying:
        - With threading enabled the session is keyed on
          ``thread_ts or ts`` (so all replies in the same Slack thread share
          a session).
        - Without threading the session is keyed on the user ID.
        """
        user_id: str = str(event.get("user", ""))
        channel_id: str = str(event.get("channel", ""))
        text: str = event.get("text", "")
        ts: str = str(event.get("ts", ""))
        thread_ts: Optional[str] = event.get("thread_ts") or None

        if not text.strip():
            return

        # Check Slack allowlist / denylist
        slack_cfg = (
            self.config.channels.slack
            if self.config.channels and self.config.channels.slack
            else None
        )
        global_denied: List[str] = [
            str(u) for u in (self.config.security.denied_users or [])
        ]
        if user_id in global_denied:
            self._logger.info(f"Slack: globally denied user {user_id}")
            return
        if slack_cfg:
            denied = [str(u) for u in (slack_cfg.denied_users or [])]
            if user_id in denied:
                self._logger.info(f"Slack: channel denied user {user_id}")
                return
            allowed = [str(u) for u in (slack_cfg.allowed_users or [])]
            if allowed and user_id not in allowed:
                self._logger.info(f"Slack: user {user_id} not in allowlist")
                return

        # Determine session key and thread reply target
        threading_enabled: bool = slack_cfg.threading if slack_cfg else False
        if threading_enabled:
            # Use the thread root as the session identifier
            session_key = thread_ts or ts
        else:
            session_key = user_id

        # Get agent + session (one active session per agent; all channels share it)
        slack_thread_id = thread_ts or ts if threading_enabled else None
        agent_id = (
            next(iter(self._agent_manager.agents))
            if self._agent_manager and self._agent_manager.agents
            else "default"
        )
        # Check for a /focus thread binding — it overrides default agent selection
        if slack_thread_id:
            bound_agent = getattr(self, "_thread_bindings", {}).get(f"slack:{slack_thread_id}")
            if bound_agent:
                agent_id = bound_agent

        session = await self._get_active_session(
            agent_id=agent_id,
            channel="slack",
            user_id=user_id,
            thread_ts=thread_ts or ts if threading_enabled else None,
        )

        # Intercept slash commands
        if text.strip().startswith("/"):
            # Fire command hook before dispatch
            cmd_name = text.strip().split()[0][1:].lower()
            await self._fire(f"command:{cmd_name}", {
                "command": cmd_name,
                "args": text.strip().split()[1:],
                "session_id": session.id if session else None,
                "channel": "slack",
                "sender_id": user_id,
            })
            from pyclopse.core.commands import CommandContext
            ctx = CommandContext(
                gateway=self,
                session=session,
                sender_id=user_id,
                channel="slack",
                thread_id=slack_thread_id,
            )
            reply = await self._command_registry.dispatch(text.strip(), ctx)
            if reply is not None:
                post_kwargs: Dict[str, Any] = {"channel": channel_id, "text": reply}
                if threading_enabled:
                    post_kwargs["thread_ts"] = thread_ts or ts
                try:
                    await slack_client.chat_postMessage(**post_kwargs)
                except Exception as e:
                    self._logger.error(f"Slack command reply failed: {e}")
            return

        # Handle message via agent
        slack_queue_key = (
            f"slack:{thread_ts or ts}"
            if threading_enabled and (thread_ts or ts)
            else f"slack:{user_id}"
        )
        response = await self.enqueue_message(
            session_key=slack_queue_key,
            content=text,
            channel="slack",
            sender=user_id,
            sender_id=user_id,
            message_id=ts,
            agent_id=agent_id,
        )

        if response:
            post_kwargs = {"channel": channel_id, "text": response}
            if threading_enabled:
                # Reply to thread (use existing thread or start one from ts)
                post_kwargs["thread_ts"] = thread_ts or ts
            try:
                await slack_client.chat_postMessage(**post_kwargs)
            except Exception as e:
                self._logger.error(f"Slack reply failed: {e}")

    async def _handle_job_command(self, text: str) -> str:
        """Parse and execute a /job command sent via Telegram.

        Commands:
          /job list                        — list all jobs
          /job add <cron> <command>        — create a new cron job
          /job del <id>                    — delete a job
          /job run <id>                    — run a job immediately
          /job help                        — show usage
        """
        if not self._job_scheduler:
            return "Job scheduler is not running."

        parts = text.split(maxsplit=2)
        # parts[0] = "/job", parts[1] = subcommand, parts[2] = rest
        sub = parts[1].lower() if len(parts) > 1 else "help"

        if sub == "help" or sub not in ("list", "add", "del", "run"):
            return (
                "Job commands:\n"
                "  /job list — list all jobs\n"
                "  /job add <cron> <command> — e.g. /job add \"0 9 * * *\" echo hello\n"
                "  /job del <id> — delete job by ID\n"
                "  /job run <id> — run job immediately\n"
                "  /job help — show this message"
            )

        if sub == "list":
            jobs = await self._job_scheduler.list_jobs()
            if not jobs:
                return "No jobs scheduled."
            lines = ["Scheduled jobs:"]
            for j in jobs:
                status = "✅" if j.enabled else "⏸"
                nxt = j.next_run.strftime("%m/%d %H:%M") if j.next_run else "—"
                # Schedule info
                s = j.schedule
                if hasattr(s, "expr"):
                    sched_str = s.expr
                elif hasattr(s, "seconds"):
                    sched_str = f"every {s.seconds}s"
                else:
                    sched_str = str(getattr(s, "at", "—"))
                # Run info
                r = j.run
                run_str = r.command[:60] if r.kind == "command" else f"[agent:{r.agent}] {r.message[:40]}"
                # Delivery target
                target_str = ""
                d = j.deliver
                if hasattr(d, "channel") and (d.channel or d.chat_id):
                    ch = d.channel or "default"
                    cid = d.chat_id or "default"
                    target_str = f"\n   target: {ch} chat={cid}"
                lines.append(
                    f"{status} [{j.id[:8]}] {j.name}\n"
                    f"   sched: {sched_str}  next: {nxt}\n"
                    f"   run: {run_str}"
                    f"{target_str}"
                )
            return "\n".join(lines)

        if sub == "add":
            # Two accepted forms:
            #   /job add "0 9 * * *" echo hello  (quoted cron — shlex gives 1st token with spaces)
            #   /job add 0 9 * * * echo hello    (unquoted — 5 bare fields then command)
            rest = parts[2] if len(parts) > 2 else ""
            import shlex
            try:
                tokens = shlex.split(rest)
            except ValueError:
                tokens = rest.split()

            cron_expr: str
            command: str
            if not tokens:
                return (
                    "Usage: /job add <cron_5fields> <command>\n"
                    'Example: /job add "0 9 * * 1-5" echo hello'
                )
            if " " in tokens[0]:
                # First token was quoted and contains the full 5-field cron
                if len(tokens) < 2:
                    return (
                        "Usage: /job add <cron_5fields> <command>\n"
                        'Example: /job add "0 9 * * 1-5" echo hello'
                    )
                cron_expr = tokens[0]
                command = " ".join(tokens[1:])
            else:
                # Unquoted: expect at least 6 tokens (5 cron fields + 1 command token)
                import re as _re
                _CRON_FIELD_RE = _re.compile(r'^[\d*/,\-]+$')

                def _looks_like_cron_field(s: str) -> bool:
                    return bool(_CRON_FIELD_RE.match(s))

                if len(tokens) < 6:
                    if len(tokens) >= 5:
                        # Check if first 5 tokens form a valid cron
                        candidate = " ".join(tokens[:5])
                        from croniter import croniter as _ci_check
                        if not _ci_check.is_valid(candidate):
                            return f"Invalid cron expression: {candidate!r}\nExpected 5 fields: min hour day month weekday"
                    elif tokens and not _looks_like_cron_field(tokens[0]):
                        # First token clearly isn't a cron field — show invalid message
                        return (
                            f"Invalid cron expression: {tokens[0]!r}\n"
                            "Expected 5 fields: min hour day month weekday"
                        )
                    return (
                        "Usage: /job add <cron_5fields> <command>\n"
                        'Example: /job add "0 9 * * 1-5" echo hello'
                    )
                cron_expr = " ".join(tokens[:5])
                command = " ".join(tokens[5:])

            from croniter import croniter as _croniter
            if not _croniter.is_valid(cron_expr):
                return f"Invalid cron expression: {cron_expr!r}\nExpected 5 fields: min hour day month weekday"

            # Parse optional delivery flags from the command string
            # e.g. --channel telegram --chat 12345
            import re as _re_job
            target_channel: Optional[str] = None
            target_chat_id: Optional[str] = None
            ch_match = _re_job.search(r"--channel\s+(\S+)", command)
            if ch_match:
                target_channel = ch_match.group(1)
                command = command[: ch_match.start()].rstrip() + command[ch_match.end() :]
            ci_match = _re_job.search(r"--chat\s+(\S+)", command)
            if ci_match:
                target_chat_id = ci_match.group(1)
                command = command[: ci_match.start()].rstrip() + command[ci_match.end() :]
            command = command.strip()

            import uuid as _uuid
            from pyclopse.jobs.models import Job, CommandRun, CronSchedule, DeliverAnnounce
            job = Job(
                id=str(_uuid.uuid4())[:8],
                name=command[:40],
                run=CommandRun(command=command),
                schedule=CronSchedule(expr=cron_expr),
                deliver=DeliverAnnounce(channel=target_channel, chat_id=target_chat_id),
            )
            await self._job_scheduler.add_job(job)
            target_info = ""
            if target_channel or target_chat_id:
                target_info = (
                    f"\n   target: {target_channel or 'default'}"
                    f" chat={target_chat_id or 'default'}"
                )
            return (
                f"✅ Job created: [{job.id}]\n"
                f"   cron: {cron_expr}\n"
                f"   cmd:  {command}"
                f"{target_info}"
            )

        if sub == "del":
            job_id_prefix = parts[2].strip() if len(parts) > 2 else ""
            if not job_id_prefix:
                return "Usage: /job del <id>"
            # Find job by prefix match
            jobs = await self._job_scheduler.list_jobs()
            matches = [j for j in jobs if j.id.startswith(job_id_prefix)]
            if not matches:
                return f"No job found matching ID: {job_id_prefix!r}"
            if len(matches) > 1:
                ids = ", ".join(j.id[:8] for j in matches)
                return f"Ambiguous ID — multiple matches: {ids}"
            job = matches[0]
            await self._job_scheduler.remove_job(job.id)
            return f"🗑 Deleted job [{job.id[:8]}] {job.name}"

        if sub == "run":
            job_id_prefix = parts[2].strip() if len(parts) > 2 else ""
            if not job_id_prefix:
                return "Usage: /job run <id>"
            jobs = await self._job_scheduler.list_jobs()
            matches = [j for j in jobs if j.id.startswith(job_id_prefix)]
            if not matches:
                return f"No job found matching ID: {job_id_prefix!r}"
            if len(matches) > 1:
                ids = ", ".join(j.id[:8] for j in matches)
                return f"Ambiguous ID — multiple matches: {ids}"
            job = matches[0]
            await self._job_scheduler.run_job_now(job.id)
            return f"▶️ Running job [{job.id[:8]}] {job.name} (result will be notified)"

        return "Unknown subcommand. Try /job help."

    @staticmethod
    def _split_message(text: str, limit: int = 4000) -> List[str]:
        """Split *text* into chunks of at most *limit* characters.

        Tries to split on paragraph boundaries (double newline) first,
        then on single newlines, then hard-splits at *limit*.
        Avoids splitting inside a fenced code block (``` ... ```).
        """
        if len(text) <= limit:
            return [text]

        chunks: List[str] = []
        remaining = text

        while len(remaining) > limit:
            # Try to split at a paragraph break within the window
            window = remaining[:limit]
            split_pos = window.rfind("\n\n")
            if split_pos > limit // 2:
                chunk = remaining[:split_pos].rstrip()
                remaining = remaining[split_pos:].lstrip()
            else:
                # Try single newline
                split_pos = window.rfind("\n")
                if split_pos > limit // 2:
                    chunk = remaining[:split_pos].rstrip()
                    remaining = remaining[split_pos:].lstrip()
                else:
                    # Hard split
                    chunk = remaining[:limit]
                    remaining = remaining[limit:]
            chunks.append(chunk)

        if remaining.strip():
            chunks.append(remaining.strip())
        return chunks

    async def start(self) -> None:
        """Start the gateway."""
        if self._is_running:
            self._logger.warning("Gateway already running")
            return

        await self.initialize()

        self._is_running = True
        self._logger.info("pyclopse Gateway started")

        # Telegram polling is now managed by TelegramPlugin.start()

        # Keep running
        try:
            while self._is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self._logger.info("Gateway cancelled")

    async def start_mcp_server(self, host: str = "0.0.0.0", port: int = 8081) -> None:
        """Start the pyclopse MCP HTTP server as a managed background task.

        Kills any process already holding the port so restarts are clean.
        Uses FastMCP's run_http_async which manages its own uvicorn lifecycle.
        """
        # Kill any stale process on the port before binding
        try:
            import subprocess as _sp
            result = _sp.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True,
            )
            for pid_str in result.stdout.strip().splitlines():
                try:
                    import os as _os
                    import signal as _signal
                    _os.kill(int(pid_str), _signal.SIGTERM)
                    self._logger.info(f"Killed stale MCP server process {pid_str} on port {port}")
                except Exception:
                    pass
            if result.stdout.strip():
                await asyncio.sleep(0.5)
        except Exception as e:
            self._logger.debug(f"Port cleanup check failed: {e}")

        from pyclopse.tools.server import mcp as pyclopse_mcp

        async def _run():
            try:
                await pyclopse_mcp.run_http_async(host=host, port=port, show_banner=False)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._logger.error(f"MCP server error: {e}")

        self._mcp_server_task = asyncio.create_task(_run(), name="pyclopse-mcp-server")
        self._logger.info(f"MCP server started on {host}:{port}")

    async def stop_mcp_server(self) -> None:
        """Stop the managed MCP server task."""
        if self._mcp_server_task and not self._mcp_server_task.done():
            # Silence expected CancelledError / incomplete-response noise from
            # uvicorn and starlette during task cancellation.
            import logging
            _noisy = ["uvicorn", "uvicorn.error", "starlette.routing"]
            _saved = {n: logging.getLogger(n).level for n in _noisy}
            for n in _noisy:
                logging.getLogger(n).setLevel(logging.CRITICAL)
            self._mcp_server_task.cancel()
            try:
                await self._mcp_server_task
            except asyncio.CancelledError:
                pass
            finally:
                for n, lvl in _saved.items():
                    logging.getLogger(n).setLevel(lvl)
            self._logger.info("MCP server stopped")
        self._mcp_server_task = None

    async def start_api_server(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the REST API server as a managed background task."""
        import uvicorn
        from pyclopse.api.app import create_app

        api_app = create_app(self)
        uv_config = uvicorn.Config(api_app, host=host, port=port, log_level="warning")
        self._api_uvicorn_server = uvicorn.Server(uv_config)

        async def _run():
            try:
                await self._api_uvicorn_server.serve()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._logger.error(f"API server error: {e}")

        self._api_server_task = asyncio.create_task(_run(), name="pyclopse-api-server")
        self._logger.info(f"REST API server started on {host}:{port}")

    async def stop_api_server(self) -> None:
        """Stop the managed REST API server task."""
        if hasattr(self, "_api_uvicorn_server") and self._api_uvicorn_server:
            self._api_uvicorn_server.should_exit = True
        if self._api_server_task and not self._api_server_task.done():
            try:
                await asyncio.wait_for(self._api_server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._api_server_task.cancel()
                try:
                    await self._api_server_task
                except asyncio.CancelledError:
                    pass
            self._logger.info("API server stopped")
        self._api_server_task = None
        self._api_uvicorn_server = None

    async def stop(self) -> None:
        """Stop the gateway."""
        self._logger.info("Stopping pyclopse Gateway...")
        await self._fire(HookEvent.GATEWAY_SHUTDOWN, {})

        self._is_running = False

        # Telegram polling is now stopped by TelegramPlugin.stop() (via channel plugin loop below)

        # Stop agents
        if self._agent_manager:
            await self.agent_manager.stop_all()

        # Stop session manager
        if self._session_manager:
            await self.session_manager.stop()

        # Stop file watcher before job scheduler (avoids a reload racing with shutdown)
        if getattr(self, "_file_watcher", None):
            await self._file_watcher.stop()
            self._file_watcher = None

        # Stop job scheduler
        if self._job_scheduler:
            await self.job_scheduler.stop()

        # Stop channel plugins
        for name, channel in list(self._channels.items()):
            try:
                await channel.stop()
                self._logger.debug(f"Channel plugin '{name}' stopped")
            except Exception as exc:
                self._logger.warning(f"Channel plugin '{name}' stop error: {exc}")

        # Stop usage monitors
        from pyclopse.core.usage import get_registry
        await get_registry().stop_all()

        # Stop MCP and API servers
        await self.stop_mcp_server()
        await self.stop_api_server()

        self._logger.info("pyclopse Gateway stopped")

    async def _get_or_create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        ephemeral: bool = False,
    ) -> Optional[Any]:
        """Wrapper around SessionManager.get_or_create_session that fires session:created.
        Used for job sessions and backwards-compat paths."""
        session = await self.session_manager.get_or_create_session(
            agent_id=agent_id, channel=channel, user_id=user_id, ephemeral=ephemeral
        )
        if session and session.id not in self._known_session_ids:
            self._known_session_ids.add(session.id)
            await self._fire(HookEvent.SESSION_CREATED, {
                "session_id": session.id,
                "agent_id": session.agent_id,
                "channel": session.channel,
                "user_id": session.user_id,
            })
        return session

    async def _get_active_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
        thread_ts: Optional[str] = None,
    ) -> Optional[Any]:
        """Get or create the single active session for this agent, update routing fields.

        All channels share one session per agent. channel/user_id/thread_ts are
        stored as last_* fields so replies can be routed back correctly.
        """
        session = await self.session_manager.get_active_session(agent_id)
        is_new = session is None
        if is_new:
            session = await self.session_manager.create_session(
                agent_id=agent_id,
                channel=channel,
                user_id=user_id,
            )
            if session:
                self.session_manager.set_active_session(agent_id, session.id)

        if session is None:
            return None

        # Restore persisted channel endpoints into the in-memory cache so
        # fan-out works even after a gateway restart.
        for _ch, _ep in session.context.get("channel_endpoints", {}).items():
            self._known_endpoints.setdefault(agent_id, {}).setdefault(_ch, _ep)

        # Update routing fields so replies go back via the right channel
        session.last_channel = channel
        session.last_user_id = user_id
        session.last_thread_ts = thread_ts
        session.save_metadata()

        if is_new or session.id not in self._known_session_ids:
            self._known_session_ids.add(session.id)
            await self._fire(HookEvent.SESSION_CREATED, {
                "session_id": session.id,
                "agent_id": session.agent_id,
                "channel": channel,
                "user_id": user_id,
            })
        return session

    async def _deliver_to_channel(self, session: Any, text: str) -> None:
        """Send text directly to the user's channel without involving the agent LLM.

        Extracts the channel type and recipient from the session's routing fields
        (``last_channel`` / ``last_user_id``) and posts ``text`` verbatim via the
        appropriate transport (Telegram bot API, Slack Web API, etc.).

        Does NOT update session history.  History injection is handled separately
        by ``_deliver_result`` before this method is called (for the verbatim
        delivery path) or is omitted entirely (for status notifications fired by
        ``_job_notify``).

        Use ``_deliver_result`` for job/subagent result delivery — it injects into
        history and then calls this method for the verbatim token path.  Call this
        directly only for operational notifications that should not appear in the
        agent's conversation history (e.g. "✅ Job started").
        """
        channel = session.last_channel or session.channel
        user_id = session.last_user_id or session.user_id
        thread_ts = getattr(session, "last_thread_ts", None)

        if channel in self._channels:
            from pyclopse.channels.base import MessageTarget
            plugin = self._channels[channel]
            target = MessageTarget(channel=channel, user_id=user_id)
            # Look up bot_name from endpoint so the correct bot delivers
            ep = self._known_endpoints.get(session.agent_id, {}).get(channel, {})
            bot_name = ep.get("bot_name")
            try:
                limit = getattr(getattr(plugin, "capabilities", None), "max_message_length", 4096)
                for chunk in self._split_message(text, limit):
                    await plugin.send_message(target, chunk, bot_name=bot_name)
            except Exception as e:
                self._logger.error(f"_deliver_to_channel {channel} plugin failed: {e}")
        elif channel == "slack":
            if self._slack_web_client:
                try:
                    kwargs: dict = {"channel": user_id, "text": text}
                    if thread_ts:
                        kwargs["thread_ts"] = thread_ts
                    await self._slack_web_client.chat_postMessage(**kwargs)
                except Exception as e:
                    self._logger.error(f"_deliver_to_channel slack failed: {e}")

    async def _deliver_result(
        self,
        label: str,
        agent_id: str,
        session: Any,
        response: str,
    ) -> None:
        """Deliver a job or subagent result to a session using token-based routing.

        Parses the delivery token from ``response`` and routes accordingly:

        - ``NO_REPLY``  — inject raw response into history; nothing sent to user
        - ``SUMMARIZE`` — inject content into history; agent LLM summarizes and relays
        - verbatim (default) — inject into history and send raw text to channel

        Both ``report_to_agent`` (scheduled jobs) and ``report_to_session``
        (subagents) converge here so all delivery paths share identical behaviour.

        Args:
            label: Human-readable name for the job or subagent (used in history labels).
            agent_id: ID of the agent that owns the target session.
            session: The resolved target session.
            response: Full response string from the job or subagent run.
        """
        token, content = _parse_job_token(response)

        async def _inject(text: str) -> None:
            """Write result turns into the target session's conversation history."""
            if not self._agent_manager:
                return
            target_agent = self._agent_manager.get_agent(agent_id)
            if not target_agent:
                return
            turns = _build_job_tool_turns(label, text)
            target_runner = target_agent._session_runners.get(session.id)
            if target_runner:
                await target_runner.inject_turns(turns)
                return
            history_path = getattr(session, "history_path", None)
            if history_path:
                await _inject_turns_to_disk(history_path, turns, label, self._logger)

        channel = session.last_channel or session.channel
        user_id = session.last_user_id or session.user_id

        if token == "NO_REPLY":
            await _inject(response)
            self._logger.info(f"{label}: NO_REPLY — delivery suppressed")

        elif token == "SUMMARIZE":
            await _inject(content)
            await self.handle_message(
                channel=channel,
                sender="job-scheduler",
                sender_id=user_id,
                content=(
                    f"[System] '{label}' results below. Relay these to the user now — "
                    f"include the actual data, not commentary about the delivery:\n\n{content}"
                ),
                agent_id=agent_id,
                dispatch_response=True,
            )

        else:  # verbatim
            await _inject(response)
            await self._deliver_to_channel(session, response)

    async def _deliver_to_spawning_session(
        self,
        job: Any,
        agent_run: Any,
        response: Optional[str],
        prefix: str = "📋",
    ) -> None:
        """Deliver a subagent result back to the session that spawned it.

        Routes through ``_deliver_result`` so token-based delivery (NO_REPLY,
        SUMMARIZE, verbatim) works identically to the ``report_to_agent`` path.

        For sub-subagents whose spawning session is a job-channel session,
        delivery is skipped — the parent subagent retrieves results synchronously
        via ``subagent_await()``.  The result is cached in the scheduler's
        ``_subagent_results`` dict by ``_run_job`` before this method is called.
        """
        if not response:
            return
        report_session_id = getattr(agent_run, "report_to_session", None)
        if not report_session_id or not self._session_manager:
            return
        try:
            target_session = await self._session_manager.get_session(report_session_id)
            if not target_session:
                self._logger.warning(
                    f"report_to_session: session {report_session_id[:8]}… not found"
                )
                return
            label = getattr(job, "name", job.id)
            channel = target_session.last_channel or target_session.channel
            agent_id = target_session.agent_id
            # Skip delivery to job-channel sessions (sub-subagents).
            # The parent subagent retrieves the result via subagent_await() instead,
            # avoiding _run_lock contention that would occur with handle_message().
            # If report_to_agent is set, fall back to that agent's live user-facing
            # session so results from job-spawned subagents are not silently dropped.
            if channel == "job":
                fallback_agent = getattr(agent_run, "report_to_agent", None)
                if fallback_agent and self._session_manager:
                    self._logger.info(
                        f"report_to_session: job-channel session — falling back to "
                        f"report_to_agent={fallback_agent!r} for {label}"
                    )
                    try:
                        fallback_session = await self._session_manager.get_active_session(fallback_agent)
                        if fallback_session:
                            await self._deliver_result(label, fallback_agent, fallback_session, response)
                        else:
                            self._logger.warning(
                                f"report_to_agent fallback: no active session for {fallback_agent!r}, result dropped"
                            )
                    except Exception as _fe:
                        self._logger.error(f"report_to_agent fallback delivery failed: {_fe}")
                else:
                    self._logger.debug(
                        f"report_to_session: skipping delivery to job-channel session "
                        f"{report_session_id[:8]}… (result cached for subagent_await)"
                    )
                return
            await self._deliver_result(label, agent_id, target_session, response)
        except Exception as _e:
            self._logger.error(f"report_to_session delivery failed: {_e}")

    async def enqueue_message(
        self,
        session_key: str,
        content: str,
        agent_id: Optional[str] = None,
        **handle_kwargs,
    ) -> Optional[str]:
        """Route an inbound message through the per-session message queue.

        *session_key* is the channel-scoped routing key (e.g. ``"telegram:12345"``
        or ``"slack:T123/U456"``).  *handle_kwargs* are forwarded verbatim to
        ``Gateway.handle_message()``.
        """
        resolved_agent_id = agent_id or (
            next(iter(self._agent_manager.agents))
            if self._agent_manager and self._agent_manager.agents
            else "default"
        )
        agent = self._agent_manager.get_agent(resolved_agent_id) if self._agent_manager else None

        from pyclopse.config.schema import QueueConfig
        base_config: QueueConfig = (
            agent.config.queue
            if agent and hasattr(agent.config, "queue")
            else QueueConfig()
        )

        async def _dispatch(msg_content: str, **kwargs) -> Optional[str]:
            return await self.handle_message(content=msg_content, **kwargs)

        queue = self._queue_manager.get_or_create(
            session_key=session_key,
            base_config=base_config,
            dispatch_fn=_dispatch,
        )
        future = await queue.enqueue(content, agent_id=resolved_agent_id, **handle_kwargs)
        # Register the drain task so /stop can cancel it
        if queue._drain_task and not queue._drain_task.done():
            self._active_tasks[session_key] = queue._drain_task
        try:
            return await future
        finally:
            self._active_tasks.pop(session_key, None)

    # ── Cross-channel event bus ───────────────────────────────────────────────

    def subscribe_agent(self, agent_id: str) -> "asyncio.Queue[dict]":
        """Subscribe to all message events for an agent. Returns a drain queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._agent_listeners.setdefault(agent_id, []).append(q)
        return q

    def unsubscribe_agent(self, agent_id: str, q: "asyncio.Queue[dict]") -> None:
        """Remove a subscriber queue registered via subscribe_agent()."""
        lst = self._agent_listeners.get(agent_id, [])
        try:
            lst.remove(q)
        except ValueError:
            pass

    def _publish(self, agent_id: str, event: dict) -> None:
        """Publish an event to all TUI/subscriber queues for this agent (non-blocking).

        The event bus is the backbone of cross-channel sync for the TUI.  Every
        inbound message and outbound response is published here so the TUI chat
        view can display activity from all channels in a single unified log.

        Event types:

        ``user_message``
            A user sent a message on some channel.  The TUI shows these for
            non-TUI channels so the operator can follow conversations happening
            elsewhere (e.g. a Telegram message appearing in the TUI chat log).
            Only published when ``agent.config.channel_sync`` is True, except
            for TUI-originating messages which are always published so the
            streaming state machine can track them.

        ``agent_response``
            The agent replied.  Published after the full response is assembled.
            For TUI-originating messages this also signals the end of a streaming
            session so ``_display_event`` can reset the live-streaming display.
            Only published when ``agent.config.channel_sync`` is True, except
            for TUI-originating responses.

        ``stream_chunk``
            One incremental text chunk from a streaming LLM response.  Published
            by the ``_bus_chunk`` closure created in ``handle_message`` for every
            non-job channel.  The TUI renders chunks from its own session
            (``originating_channel == "tui"``) in real time via ``_drain_events``;
            chunks from other channels are ignored by the TUI renderer.

        Delivery is best-effort: if a subscriber's queue is full the event is
        dropped rather than blocking the caller.

        Args:
            agent_id (str): Agent whose subscribers should receive the event.
            event (dict): Arbitrary dict; must include a ``"type"`` key.
        """
        for q in list(self._agent_listeners.get(agent_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer — drop rather than block

    async def _fan_out_user_message(
        self,
        session: Any,
        originating_channel: str,
        sender: str,
        content: str,
    ) -> None:
        """Forward an inbound user message to every other channel endpoint.

        Part of the cross-channel sync system.  When a user sends a message on
        any channel, this method delivers the raw content to all other channels
        that have previously interacted with this agent so every party sees the
        full conversation.  Messages are delivered natively — no source prefix or
        channel label — so they appear as if typed locally in the target channel.

        Called as a fire-and-forget ``asyncio.create_task`` from
        ``handle_message`` so Telegram/Slack network I/O does not block the LLM
        call.  Only invoked when ``agent.config.channel_sync`` is True.

        The TUI and job channels are excluded: the TUI receives all activity via
        the event bus (``_publish``), and job channels are ephemeral.

        Endpoint lookup merges the gateway-level ``_known_endpoints`` cache
        (updated on every inbound message) with ``session.context["channel_endpoints"]``
        (persisted to disk so endpoints survive restarts).  The stored
        ``bot_name`` field is used to select the exact Telegram bot the user is
        conversing with rather than falling back to the first configured bot.

        Args:
            session: Active session for the agent.
            originating_channel (str): Channel the message arrived on (excluded
                from fan-out to avoid echo).
            sender (str): Display name of the sender.
            content (str): Raw message text to forward.
        """
        gw_eps = self._known_endpoints.get(session.agent_id, {})
        sess_eps = session.context.get("channel_endpoints", {})
        endpoints: Dict[str, dict] = dict(gw_eps)
        endpoints.update(sess_eps)
        for ch, ep in endpoints.items():
            if ch in (originating_channel, "tui", "job"):
                continue
            sender_id = ep.get("sender_id", "")
            if ch in self._channels:
                plugin = self._channels[ch]
                try:
                    from pyclopse.channels.base import MessageTarget
                    target = MessageTarget(channel=ch, user_id=sender_id)
                    await plugin.send_message(target, content, bot_name=ep.get("bot_name"))
                except Exception as e:
                    self._logger.error(f"fan-out user message {ch} plugin error: {e}")
            elif ch == "slack":
                if self._slack_web_client:
                    try:
                        await self._slack_web_client.chat_postMessage(
                            channel=sender_id,
                            text=content,
                        )
                    except Exception as e:
                        self._logger.error(f"fan-out user message slack error: {e}")

    async def _fan_out_response(
        self,
        session: Any,
        originating_channel: str,
        response_text: str,
    ) -> None:
        """Deliver an agent response to every channel endpoint OTHER than the originator.

        Part of the cross-channel sync system.  After the agent produces a
        response, this method sends it to every other channel that has previously
        interacted with this agent.  Responses appear natively — no source label —
        as if the agent replied directly in each channel.  Only invoked when
        ``agent.config.channel_sync`` is True.

        **Thinking formatting** is handled per-channel:

        - *Telegram*: ``format_thinking_for_telegram()`` is called first.  If the
          response contains ``<thinking>`` blocks (either inline tags or blocks
          reconstructed from ``is_reasoning=True`` stream chunks), they are
          rendered as an expandable ``<blockquote>`` spoiler.  Falls back to
          ``strip_thinking_tags()`` for plain delivery when no thinking is present.
        - *Slack*: ``strip_thinking_tags()`` always applied — Slack does not
          support the Telegram HTML thinking format.

        The TUI and job channels are excluded: the TUI receives the agent response
        via the ``agent_response`` event bus event published by the caller, and
        job channels are ephemeral.

        Endpoint lookup merges the gateway-level ``_known_endpoints`` cache with
        ``session.context["channel_endpoints"]`` (session wins on conflict).  The
        stored ``bot_name`` is used for Telegram so the response is sent via the
        exact bot the user is conversing with.

        Args:
            session: Active session for the agent.
            originating_channel (str): Channel the conversation started on
                (excluded from fan-out to avoid echo).
            response_text (str): Full agent response, potentially including
                ``<thinking>`` blocks if ``show_thinking`` is enabled.
        """
        # Merge gateway-level cache with session-level endpoints; session wins
        gw_eps = self._known_endpoints.get(session.agent_id, {})
        sess_eps = session.context.get("channel_endpoints", {})
        endpoints: Dict[str, dict] = dict(gw_eps)
        endpoints.update(sess_eps)
        self._logger.info(
            "fan-out: agent=%s originating=%s gw_endpoints=%s sess_endpoints=%s merged=%s",
            session.agent_id, originating_channel,
            list(gw_eps.keys()), list(sess_eps.keys()), list(endpoints.keys()),
        )
        for ch, ep in endpoints.items():
            if ch in (originating_channel, "tui", "job"):
                self._logger.info("fan-out: skipping channel=%s (excluded)", ch)
                continue
            sender_id = ep.get("sender_id", "")
            if ch in self._channels:
                # Plugin-based delivery
                plugin = self._channels[ch]
                try:
                    from pyclopse.channels.base import MessageTarget
                    from pyclopse.agents.runner import strip_thinking_tags
                    target = MessageTarget(channel=ch, user_id=sender_id)
                    # Channels with HTML support get thinking-formatted text;
                    # others get clean text with thinking stripped.
                    if getattr(getattr(plugin, "capabilities", None), "html_formatting", False):
                        from pyclopse.agents.runner import format_thinking_for_telegram
                        formatted = format_thinking_for_telegram(response_text)
                        if formatted:
                            await plugin.send_message(target, formatted, parse_mode="HTML",
                                                      bot_name=ep.get("bot_name"))
                        else:
                            clean = strip_thinking_tags(response_text)
                            limit = getattr(plugin.capabilities, "max_message_length", 4096)
                            for chunk in self._split_message(clean, limit):
                                await plugin.send_message(target, chunk,
                                                          bot_name=ep.get("bot_name"))
                    else:
                        clean = strip_thinking_tags(response_text)
                        limit = getattr(getattr(plugin, "capabilities", None), "max_message_length", 4096)
                        for chunk in self._split_message(clean, limit):
                            await plugin.send_message(target, chunk,
                                                      bot_name=ep.get("bot_name"))
                    self._logger.info("fan-out: delivered to %s via plugin sender_id=%s", ch, sender_id)
                except Exception as e:
                    self._logger.error(f"fan-out {ch} plugin error: {e}")
            elif ch == "slack":
                if self._slack_web_client:
                    try:
                        from pyclopse.agents.runner import strip_thinking_tags
                        await self._slack_web_client.chat_postMessage(
                            channel=sender_id,
                            text=strip_thinking_tags(response_text),
                        )
                    except Exception as e:
                        self._logger.error(f"fan-out slack error: {e}")

    async def handle_message(
        self,
        channel: str,
        sender: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        dispatch_response: bool = False,
        on_chunk: Optional[Callable[[str, bool], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """Handle an inbound message from any channel and return the agent reply.

        This is the single entry point for all non-Telegram-streaming inbound
        messages (TUI, Slack, HTTP API, job responses, subagents).  Telegram
        messages that use streaming go through ``_stream_telegram_response``
        instead; everything else routes here.

        **Full pipeline:**

        1. Deduplication (``message_id``-based, TTL 60 s)
        2. Session lookup — ``_get_active_session`` for normal channels,
           ``_get_or_create_session`` for job channel
        3. Hook: ``message:received``
        4. Allowlist / denylist / activation-mode checks
        5. Command dispatch (``/slash`` commands short-circuit here)
        6. Agent lookup + ``agent:bootstrap`` hook on first message
        7. Endpoint registration — merges into ``_known_endpoints`` and
           ``session.context["channel_endpoints"]`` preserving ``bot_name``
        8. Event bus publish (``user_message``) — gated on ``channel_sync``
        9. Cross-channel user-message fan-out — fire-and-forget task, gated
           on ``channel_sync``
        10. ``_bus_chunk`` closure — always created for non-job channels;
            publishes ``stream_chunk`` events to the event bus so the TUI can
            render streaming output on its 0.3 s drain timer.  Any
            caller-supplied ``on_chunk`` is also called from within
            ``_bus_chunk``.
        11. ``agent.handle_message`` — runs the LLM, fires chunks via
            ``_bus_chunk``
        12. Token snapshot, send-policy check
        13. Event bus publish (``agent_response``) — gated on ``channel_sync``
            except for ``channel == "tui"`` (needed to reset streaming state)
        14. Hook: ``agent:after_response``
        15. Usage counters, audit log, ``message:sent`` hook
        16. Optional ``dispatch_response`` direct delivery
        17. Cross-channel response fan-out — ``_fan_out_response``, gated on
            ``channel_sync``

        Args:
            channel (str): Originating channel identifier (``"tui"``,
                ``"slack"``, ``"http"``, ``"job"``, etc.).
            sender (str): Human-readable sender display name.
            sender_id (str): Stable identifier for the sender (used as session
                key and fan-out address).
            content (str): Raw message text.
            message_id (Optional[str]): Deduplication key; omit for channels
                that don't provide message IDs.
            agent_id (Optional[str]): Target agent; defaults to the first
                configured agent.
            dispatch_response (bool): If True, also deliver the reply via
                ``_deliver_to_channel`` after returning.
            on_chunk (Optional[Callable]): Async callback ``(text, is_reasoning)``
                fired for each streaming chunk in addition to the event-bus
                publish.  Pass ``None`` (the default) to rely solely on the
                event bus — the TUI does this.

        Returns:
            Optional[str]: Agent reply text, or None if suppressed by
                send-policy, activation-mode, or a slash command.
        """
        # Create incoming message
        message = IncomingMessage(
            id=message_id or "",
            channel=channel,
            sender=sender,
            sender_id=sender_id,
            content=content,
        )

        # Use the provided agent_id or fall back to the first configured agent
        if not agent_id:
            agent_id = (
                next(iter(self._agent_manager.agents))
                if self._agent_manager and self._agent_manager.agents
                else "default"
            )

        # Get the agent's active session (one per agent; all channels share it).
        # Job channel uses the old isolated-session path.
        if channel == "job":
            session = await self._get_or_create_session(
                agent_id=agent_id,
                channel=channel,
                user_id=sender_id,
            )
        else:
            session = await self._get_active_session(
                agent_id=agent_id,
                channel=channel,
                user_id=sender_id,
            )

        if session is None:
            return "Could not create session"

        # Fire message:received
        await self._fire(HookEvent.MESSAGE_RECEIVED, {
            "channel": channel,
            "sender": sender,
            "sender_id": sender_id,
            "content": content,
            "session_id": session.id,
            "agent_id": session.agent_id,
        })

        # Log message
        if self._audit_logger:
            await self._audit_logger.log_message_received(
                session_id=session.id,
                agent_id=session.agent_id,
                channel=channel,
                user_id=sender_id,
                message_preview=content[:100],
            )

        # Check activation_mode — "mention" requires the agent name in the message
        activation_mode = session.context.get("activation_mode", "always")
        if activation_mode == "mention":
            agent_name_hint = session.agent_id.lower()
            if agent_name_hint not in content.lower():
                self._logger.debug(
                    f"activation_mode=mention: skipping message without mention of {agent_name_hint!r}"
                )
                return None

        # Get agent
        agent = self.agent_manager.get_agent(session.agent_id)
        if agent is None:
            return "No agent available"

        # Fire message:preprocessed — all checks passed, message is about to reach the agent
        await self._fire(HookEvent.MESSAGE_PREPROCESSED, {
            "body_for_agent": content,
            "channel": channel,
            "sender_id": sender_id,
            "session_id": session.id,
            "agent_id": session.agent_id,
            "transcript": None,  # populated when voice input is added
        })

        # Fire agent:bootstrap when a session runner is being created for the first time
        is_new_runner = session.id not in agent._session_runners
        if is_new_runner:
            from pyclopse.core.prompt_builder import BOOTSTRAP_FILES, get_agent_dir
            agent_dir = get_agent_dir(agent.id, agent.config_dir)
            loaded_files = [
                str(agent_dir / f)
                for f in BOOTSTRAP_FILES
                if (agent_dir / f).exists()
            ]
            await self._fire(HookEvent.AGENT_BOOTSTRAP, {
                "agent_id": agent.id,
                "session_id": session.id,
                "workspace_dir": str(agent_dir),
                "bootstrap_files": loaded_files,
            })

        # Track per-channel delivery endpoint and publish inbound event to all subscribers.
        # Merge into existing entry rather than replacing, so extra fields like bot_name
        # (set by the Telegram handler before routing) are preserved.
        if channel not in ("job",):
            _gw_ep = self._known_endpoints.setdefault(agent_id, {}).setdefault(channel, {})
            _gw_ep["sender_id"] = sender_id
            _gw_ep["sender"] = sender
            _sess_ep = session.context.setdefault("channel_endpoints", {}).setdefault(channel, {})
            _sess_ep["sender_id"] = sender_id
            _sess_ep["sender"] = sender
            self._logger.info(
                "endpoint registered: agent=%s channel=%s sender_id=%s known=%s",
                agent_id, channel, sender_id, list(self._known_endpoints.get(agent_id, {}).keys()),
            )
            _channel_sync = getattr(getattr(agent, "config", None), "channel_sync", True)
            # Always publish TUI's own messages (needed for streaming state); gate
            # cross-channel publishes on channel_sync.
            if _channel_sync or channel == "tui":
                self._publish(agent_id, {
                    "type": "user_message",
                    "channel": channel,
                    "sender": sender,
                    "content": content,
                })
            # Forward the user's message to every other channel so all parties
            # see the full conversation, not just the agent's replies.
            # Fire-and-forget: don't block the LLM call on Telegram/Slack network I/O.
            if _channel_sync:
                asyncio.create_task(
                    self._fan_out_user_message(
                        session, originating_channel=channel, sender=sender, content=content,
                    )
                )

        # For non-job channels, always stream via event bus so all subscribers
        # (including the TUI) get live chunk updates without needing an on_chunk
        # callback from the caller.  If the caller also provides on_chunk, it is
        # called in addition to the event-bus publish.
        _effective_on_chunk = on_chunk
        if channel not in ("job",):
            _agent_ref = agent
            _caller_on_chunk = on_chunk
            async def _bus_chunk(chunk_text: str, is_reasoning: bool) -> None:
                self._publish(agent_id, {
                    "type": "stream_chunk",
                    "chunk": chunk_text,
                    "is_reasoning": is_reasoning,
                    "agent_name": _agent_ref.name,
                    "originating_channel": channel,
                })
                if _caller_on_chunk is not None:
                    await _caller_on_chunk(chunk_text, is_reasoning)
            _effective_on_chunk = _bus_chunk

        # Handle message
        response = await agent.handle_message(message, session, on_chunk=_effective_on_chunk)
        response_text = response.content if response else None
        self._logger.info(
            "handle_message: channel=%s agent=%s response_len=%s preview=%r",
            channel, agent_id,
            len(response_text) if response_text else None,
            (response_text or "")[:80],
        )

        # Snapshot context token count into session after each response so that
        # /status can display it even before the runner is next accessed.
        _snapshot_ctx_tokens(agent, session)

        # Check send_policy — "off" suppresses the outbound reply
        if session.context.get("send_policy") == "off":
            response_text = None

        # Publish agent response to event bus (after send_policy so suppressed replies don't broadcast).
        # Always publish for TUI channel (needed to reset streaming state); gate others on channel_sync.
        if response_text and channel not in ("job",):
            _channel_sync = getattr(getattr(agent, "config", None), "channel_sync", True)
            if _channel_sync or channel == "tui":
                self._publish(agent_id, {
                    "type": "agent_response",
                    "agent_name": agent.name,
                    "content": response_text,
                    "originating_channel": channel,
                })

        # Fire agent:after_response
        await self._fire(HookEvent.AGENT_RESPONSE, {
            "agent_id": agent.id,
            "session_id": session.id,
            "channel": channel,
            "response": response_text,
        })

        # Increment usage counters
        self._usage["messages_total"] += 1
        self._usage["messages_by_agent"][agent_id] = (
            self._usage["messages_by_agent"].get(agent_id, 0) + 1
        )
        self._usage["messages_by_channel"][channel] = (
            self._usage["messages_by_channel"].get(channel, 0) + 1
        )

        if response and self._audit_logger:
            await self._audit_logger.log(
                event_type="message_sent",
                agent_id=agent.id,
                session_id=session.id,
                channel=channel,
                user_id=sender_id,
            )

        # Fire message:sent
        await self._fire(HookEvent.MESSAGE_SENT, {
            "channel": channel,
            "session_id": session.id,
            "agent_id": agent.id,
            "response": response_text,
        })

        # Auto-dispatch the response back via the session's channel when requested
        if dispatch_response and response_text and session:
            await self._deliver_to_channel(session, response_text)

        # Fan out to every other channel that has interacted with this session
        if response_text and session and channel not in ("job",):
            if getattr(getattr(agent, "config", None), "channel_sync", True):
                await self._fan_out_response(session, originating_channel=channel, response_text=response_text)

        return response_text

    async def reload_config(self) -> Dict[str, Any]:
        """Reload configuration from disk and apply non-destructive changes.

        Returns a dict of field names that were changed.
        """
        old_config = self._config
        self._config = self._config_loader.load()
        self._logger.info("Configuration reloaded from disk")

        changed: Dict[str, Any] = {}

        if old_config is None:
            return changed

        # Apply safe, non-destructive changes immediately
        new_cfg = self._config

        # CORS origins — applied at the app level, not here; just report change
        old_cors = old_config.gateway.cors_origins
        new_cors = new_cfg.gateway.cors_origins
        if old_cors != new_cors:
            changed["gateway.cors_origins"] = {"old": old_cors, "new": new_cors}

        # Log level
        old_log = old_config.gateway.log_level
        new_log = new_cfg.gateway.log_level
        if old_log != new_log:
            import logging as _logging
            level = getattr(_logging, new_log.upper(), _logging.INFO)
            _logging.getLogger("pyclopse").setLevel(level)
            changed["gateway.log_level"] = {"old": old_log, "new": new_log}

        # Concurrency limits — rebuild from provider model configs
        old_cc = old_config.concurrency
        new_cc = new_cfg.concurrency
        old_limits = self._collect_model_limits(old_config.providers)
        new_limits = self._collect_model_limits(new_cfg.providers)
        if old_cc.default != new_cc.default or old_limits != new_limits:
            from pyclopse.core.concurrency import init_manager
            init_manager(model_limits=new_limits, default=new_cc.default)
            changed["concurrency"] = {
                "old": {"default": old_cc.default, "models": old_limits},
                "new": {"default": new_cc.default, "models": new_limits},
            }

        # Usage monitors — reinitialize if provider config changed
        try:
            from pyclopse.core.usage import init_registry, get_registry
            old_providers_dump = old_config.providers.model_dump() if old_config.providers else {}
            new_providers_dump = new_cfg.providers.model_dump() if new_cfg.providers else {}
            if old_providers_dump != new_providers_dump:
                await get_registry().stop_all()
                new_registry = init_registry(new_cfg.providers)
                await new_registry.start_all()
                changed["usage_monitors"] = "reinitialized"
                self._logger.info("Usage monitors reinitialized after provider config change")
        except Exception as _ue:
            self._logger.warning(f"Usage monitor reload failed: {_ue}")

        # Agent config changes — update runners in-place
        _am = getattr(self, "_agent_manager", None)
        if _am:
            old_agents = old_config.agents.model_dump() if old_config.agents else {}
            new_agents = new_cfg.agents.model_dump() if new_cfg.agents else {}
            for agent_id, new_agent_dict in new_agents.items():
                old_agent_dict = old_agents.get(agent_id, {})
                if old_agent_dict == new_agent_dict:
                    continue
                managed = _am.get_agent(agent_id)
                if managed is None:
                    continue
                # Find which fields changed
                agent_changes = {
                    k: {"old": old_agent_dict.get(k), "new": v}
                    for k, v in new_agent_dict.items()
                    if old_agent_dict.get(k) != v
                }
                try:
                    from pyclopse.config.schema import AgentConfig
                    managed.config = AgentConfig(**new_agent_dict)
                    # Recreate the base runner with the new config
                    from pyclopse.agents.runner import AgentRunner
                    old_runner = managed.fast_agent_runner
                    managed.fast_agent_runner = AgentRunner(
                        agent_name=managed.name,
                        instruction=managed.system_prompt,
                        model=managed.config.model or (old_runner.model if old_runner else "sonnet"),
                        temperature=managed.config.temperature,
                        max_tokens=managed.config.max_tokens,
                        servers=old_runner.servers if old_runner else None,
                        tools_config=old_runner.tools_config if old_runner else None,
                        show_thinking=getattr(managed.config, "show_thinking", False),
                        api_key=old_runner.api_key if old_runner else None,
                    )
                    # Clear per-session runners so they pick up the new base
                    managed._session_runners.clear()
                    changed[f"agents.{agent_id}"] = agent_changes
                    self._logger.info(
                        f"Agent '{agent_id}' runner recreated: {list(agent_changes.keys())}"
                    )
                except Exception as _ae:
                    self._logger.error(f"Failed to reload agent '{agent_id}': {_ae}")

        if changed:
            self._logger.info(f"Config reload applied changes: {list(changed.keys())}")
        else:
            self._logger.info("Config reload: no changes detected")

        return changed

    def get_status(self) -> Dict[str, Any]:
        """Get gateway status."""
        return {
            "is_running": self._is_running,
            "config_version": self.config.version,
            "security": {
                "audit_enabled": self._audit_logger is not None,
                "approval_mode": (
                    self._approval_system.mode.value if self._approval_system else None
                ),
                "sandbox_type": (self._config.security.sandbox.type if self._config else None),
            },
            "agents": self.agent_manager.get_status() if self._agent_manager else {},
            "sessions": self.session_manager.get_status() if self._session_manager else {},
            "jobs": self.job_scheduler.get_status() if self._job_scheduler else {},
        }


async def create_gateway(config_path: Optional[str] = None) -> Gateway:
    """Create and initialize a gateway."""
    gateway = Gateway(config_path)
    await gateway.initialize()
    return gateway
