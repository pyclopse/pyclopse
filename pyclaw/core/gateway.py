"""Main Gateway class for pyclaw."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclaw.config.loader import ConfigLoader, Config
from pyclaw.config.schema import AgentConfig, SecurityConfig
from pyclaw.hooks.events import HookEvent
from pyclaw.security.audit import AuditLogger
from pyclaw.security.approvals import ExecApprovalSystem
from pyclaw.security.sandbox import Sandbox, create_sandbox
from pyclaw.jobs.scheduler import JobScheduler
from pyclaw.core.agent import Agent, AgentManager
from pyclaw.core.session import SessionManager
from pyclaw.core.router import MessageRouter, IncomingMessage, OutgoingMessage
from pyclaw.pulse import PulseRunner, PulseTask, PulseActiveHours


class Gateway:
    """Main Gateway class that orchestrates all pyclaw subsystems."""

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

        # Core
        self._agent_manager: Optional[AgentManager] = None
        self._session_manager: Optional[SessionManager] = None
        self._router: Optional[MessageRouter] = None

        # Runtime
        self._is_running = False
        self._initialized = False
        self._startup_tasks: List[asyncio.Task] = []
        self._logger = logging.getLogger("pyclaw.gateway")

        # Channel adapters (to be implemented)
        self._channels: Dict[str, Any] = {}

        # Hook system
        self._hook_registry: Optional[Any] = None   # HookRegistry
        self._memory_service: Optional[Any] = None  # MemoryService

        # Tracks session IDs we've already seen (for session:created detection)
        self._known_session_ids: set = set()

        # Command registry
        from pyclaw.core.commands import CommandRegistry, register_builtin_commands
        self._command_registry = CommandRegistry()
        register_builtin_commands(self._command_registry, self)

        # Pulse runner for heartbeats
        self._pulse_runner: Optional[PulseRunner] = None
        self._last_pulse_result: Optional[str] = None  # For TUI to display
        # Multi-bot Telegram: keyed by bot_name ("_default" for legacy single-bot)
        self._tg_bots: Dict[str, Any] = {}
        self._tg_chat_ids: Dict[str, Optional[str]] = {}
        self._tg_polling_tasks: Dict[str, asyncio.Task] = {}
        self._slack_web_client: Optional[Any] = None  # AsyncWebClient for outbound Slack messages
        # Active processing tasks keyed by session ID (used by /stop)
        self._active_tasks: Dict[str, asyncio.Task] = {}

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
                await self._fire(HookEvent.SESSION_EXPIRED, {
                    "session_id": session.id,
                    "agent_id": session.agent_id,
                    "channel": session.channel,
                    "user_id": session.user_id,
                })

            self._session_manager = SessionManager(
                persist_dir=sc.persist_dir if sc else "~/.pyclaw/sessions",
                ttl_hours=sc.ttl_hours if sc else 24,
                reaper_interval_minutes=sc.reaper_interval_minutes if sc else 60,
                on_expire=_on_session_expire,
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

    @property
    def pulse_runner(self) -> Optional[PulseRunner]:
        """Get pulse runner."""
        return self._pulse_runner
    
    @property
    def last_pulse_result(self) -> Optional[str]:
        """Get last pulse result (for TUI display)."""
        return self._last_pulse_result
    
    def clear_pulse_result(self) -> None:
        """Clear pulse result after TUI reads it."""
        self._last_pulse_result = None

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

        self._logger.info("Initializing pyclaw Gateway...")

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

        # Initialize core
        await self._init_core()

        # Initialize channels
        await self._init_channels()

        # Initialize jobs
        await self._init_jobs()

        # Initialize TODO store
        await self._init_todos()

        # Initialize pulse runner
        await self._init_pulse()

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
        from pyclaw.hooks.registry import HookRegistry
        from pyclaw.hooks.loader import HookLoader

        self._hook_registry = HookRegistry()

        # Determine config_dir from the config file path
        config_dir = "~/.pyclaw"
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
        from pyclaw.memory.service import MemoryService, set_memory_service
        from pyclaw.memory.embeddings import make_embedding_backend

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

        if mem_cfg.backend == "clawvault":
            from pyclaw.memory.clawvault import ClawVaultBackend
            default_backend = ClawVaultBackend(
                vault_path=mem_cfg.clawvault.vault_path
            )
        else:
            # Default: per-agent file backend using a "gateway" namespace
            from pyclaw.memory.file_backend import FileMemoryBackend
            config_dir = "~/.pyclaw"
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

    async def _init_concurrency(self) -> None:
        """Initialize per-model concurrency manager from config."""
        from pyclaw.core.concurrency import init_manager
        cc = self.config.concurrency
        init_manager(model_limits=cc.models, default=cc.default)
        self._logger.info(
            f"Concurrency manager: default={cc.default}, models={cc.models or '{}'}"
        )

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
        # Session manager
        await self.session_manager.start()
        self._logger.info("Session manager started")

        # Create default agent from config
        for agent_id, agent_config_dict in self.config.agents.model_dump().items():
            name = agent_config_dict.get("name", agent_id)
            # Extract provider config if present (per-agent provider: block)
            provider_config = agent_config_dict.get("provider")
            # Merge top-level providers config for known model prefixes so that
            # api_key stored under providers.minimax flows to the agent runner.
            model_str = agent_config_dict.get("model", "")
            if "generic." in str(model_str) or "minimax" in str(model_str).lower():
                mm_cfg = self.config.providers.minimax
                if mm_cfg:
                    mm_dict = mm_cfg.model_dump()
                    if provider_config:
                        # Per-agent provider config takes precedence
                        merged = {**mm_dict, **provider_config}
                    else:
                        merged = {**mm_dict, "type": "minimax"}
                    provider_config = merged
            # Convert dict to AgentConfig object
            agent_config = AgentConfig(**agent_config_dict)

            # Get config_dir from config loader (default: ~/.pyclaw)
            # config_path could be a file like ~/.pyclaw/config/pyclaw.yaml, so get parent
            if self._config_loader.config_path:
                config_path_obj = self._config_loader.config_path
                if config_path_obj.is_file():
                    config_dir = str(config_path_obj.parent)
                else:
                    config_dir = str(config_path_obj)
            else:
                config_dir = "~/.pyclaw"

            self.agent_manager.create_agent(
                agent_id=agent_id,
                name=name,
                config=agent_config,
                provider_config=provider_config,
                session_manager=self.session_manager,
                config_dir=config_dir,
            )

        await self.agent_manager.start_all()
        self._logger.info(f"Started {len(self.agent_manager.agents)} agents")

    async def _init_channel_plugins(self) -> None:
        """Load, start, and register all channel plugins."""
        from pyclaw.channels.loader import load_all
        from pyclaw.channels.plugin import GatewayHandle

        specs = list(self.config.plugins.channels)

        # Build a GatewayHandle backed by this gateway instance
        gw = self

        class _Handle(GatewayHandle):
            async def dispatch(
                self,
                channel: str,
                user_id: str,
                user_name: str,
                text: str,
                message_id: Optional[str] = None,
            ) -> Optional[str]:
                return await gw.handle_message(
                    channel=channel,
                    sender=user_name,
                    sender_id=user_id,
                    content=text,
                    message_id=message_id,
                )

        handle = _Handle()
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
        # Load and start external channel plugins first
        await self._init_channel_plugins()

        # Initialize Telegram bot(s)
        await self._init_telegram()

        # Initialize Slack outbound client if configured
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

    async def _init_telegram(self) -> None:
        """Initialize Telegram bot(s).

        Supports two modes:
        - **Multi-bot** (``channels.telegram.bots`` dict populated): one Bot
          instance per named entry, each routed to its configured agent.
        - **Legacy single-bot** (``bot_token`` set directly on TelegramConfig):
          a single Bot stored under the synthetic key ``"_default"``.
        """
        telegram_config = self.config.channels.telegram
        token_summary = "set" if telegram_config and telegram_config.bot_token else "empty"
        self._logger.info(
            f"Telegram config: enabled={telegram_config.enabled if telegram_config else None}, "
            f"bot_token={token_summary}, "
            f"bots={list(telegram_config.bots) if telegram_config else []}"
        )
        if not telegram_config or not telegram_config.enabled:
            self._logger.info("Telegram disabled or not configured")
            return

        try:
            from telegram import Bot
        except ImportError:
            self._logger.warning("python-telegram-bot not installed, Telegram disabled")
            return

        # Build a list of (bot_name, token, effective_config) to initialize
        bots_to_init: List[tuple] = []  # (name, token, effective_cfg)

        if telegram_config.bots:
            # Multi-bot mode
            for bot_name, bot_cfg in telegram_config.bots.items():
                effective = telegram_config.effective_config_for_bot(bot_name)
                if effective.bot_token:
                    bots_to_init.append((bot_name, effective.bot_token, effective))
                else:
                    self._logger.warning(f"Telegram bot '{bot_name}' has no botToken, skipping")
        elif telegram_config.bot_token:
            # Legacy single-bot mode — use synthetic name "_default"
            bots_to_init.append(("_default", telegram_config.bot_token, telegram_config))

        for bot_name, token, effective_cfg in bots_to_init:
            try:
                bot = Bot(token=token)
                me = await bot.get_me()
                # Clear any stale webhook / long-poll session that would cause 409 Conflict
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                    self._logger.debug(f"Cleared webhook for bot '{bot_name}'")
                except Exception as wh_err:
                    self._logger.warning(f"Could not clear webhook for bot '{bot_name}': {wh_err}")
                self._tg_bots[bot_name] = bot
                # Use first allowed_users entry as default pulse target
                allowed = getattr(effective_cfg, "allowed_users", None) or []
                self._tg_chat_ids[bot_name] = str(allowed[0]) if allowed else None
                self._logger.info(
                    f"Telegram bot '{bot_name}' initialized: @{me.username} "
                    f"(agent={getattr(effective_cfg, 'agent', None) or 'first'})"
                )
                await self._register_telegram_commands_for_bot(bot)
            except Exception as e:
                self._logger.error(f"Failed to initialize Telegram bot '{bot_name}': {e}")

        if self._tg_bots:
            self._logger.info(
                f"Telegram ready: {len(self._tg_bots)} bot(s) — {list(self._tg_bots)}"
            )

    def _agent_id_for_bot(self, bot_name: str) -> str:
        """Resolve which agent_id a given bot should route messages to."""
        telegram_config = self.config.channels.telegram
        if telegram_config and telegram_config.bots and bot_name in telegram_config.bots:
            effective = telegram_config.effective_config_for_bot(bot_name)
            agent_id = effective.agent
            if agent_id:
                if self._agent_manager and agent_id in self._agent_manager.agents:
                    return agent_id
                self._logger.warning(
                    f"Telegram bot '{bot_name}' configured for agent '{agent_id}' "
                    f"but that agent is not registered — falling back to first agent"
                )
        # Fall back: first available agent
        if self._agent_manager and self._agent_manager.agents:
            return next(iter(self._agent_manager.agents))
        return "default"

    def _bot_and_chat_for_agent(self, agent_id: str) -> tuple:
        """Return (bot, chat_id) for the Telegram bot configured for ``agent_id``.

        Searches the bots dict for a bot whose ``agent`` field matches, then
        falls back to the first bot.  Returns ``(None, None)`` if no bot exists.
        """
        self._ensure_tg_dicts()
        telegram_config = self.config.channels.telegram if hasattr(self, "_config") and self._config else None
        if telegram_config and telegram_config.bots:
            for bot_name, bot_cfg in telegram_config.bots.items():
                effective = telegram_config.effective_config_for_bot(bot_name)
                if effective.agent == agent_id and bot_name in self._tg_bots:
                    return self._tg_bots[bot_name], self._tg_chat_ids.get(bot_name)
        # Fall back to first bot
        if self._tg_bots:
            bot_name = next(iter(self._tg_bots))
            return self._tg_bots[bot_name], self._tg_chat_ids.get(bot_name)
        return None, None

    async def _register_telegram_commands_for_bot(self, bot: Any) -> None:
        """Register slash commands for a specific Bot instance."""
        try:
            from telegram import BotCommand
            commands = [
                BotCommand(cmd, desc)
                for cmd, desc in self._command_registry.commands_for_telegram()
            ]
            await bot.set_my_commands(commands)
            self._logger.info(f"Registered {len(commands)} Telegram commands")
        except Exception as e:
            self._logger.warning(f"Failed to register Telegram commands: {e}")

    async def _register_telegram_commands(self) -> None:
        """Register slash commands with Telegram so they appear in the UI command picker."""
        bot = self._telegram_bot
        if not bot:
            return
        await self._register_telegram_commands_for_bot(bot)

    async def _init_jobs(self) -> None:
        """Initialize job scheduler."""
        from pyclaw.jobs.models import JobStatus, DeliverAnnounce

        async def _agent_executor(job: Any) -> dict:
            """Run an agent-type job: send message to agent, return response."""
            try:
                response = await self.handle_message(
                    message=job.run.message,
                    agent_id=job.run.agent,
                    session_id=f"job:{job.id}",
                    sender_id="job-scheduler",
                    channel="job",
                    model_override=job.run.model,
                )
                return {
                    "success": True,
                    "stdout": response or "",
                    "stderr": "",
                    "exit_code": 0,
                }
            except Exception as e:
                return {"success": False, "error": str(e), "exit_code": 1}

        async def _job_notify(job: Any, run: Any) -> None:
            """Send job completion/failure notification."""
            # Determine delivery target
            deliver = getattr(job, "deliver", None)
            if deliver and getattr(deliver, "mode", None) == "none":
                return

            # For announce mode, pick channel + chat_id
            chat_id = None
            if deliver and getattr(deliver, "mode", None) == "announce":
                chat_id = getattr(deliver, "chat_id", None)
            # Fall back to the default Telegram chat (whoever last messaged)
            chat_id = chat_id or self._telegram_chat_id

            if not self._telegram_bot or not chat_id:
                return

            ok = run.status == JobStatus.COMPLETED
            icon = "✅" if ok else "❌"
            duration = f" ({run.duration_ms():.0f}ms)" if run.duration_ms() else ""
            lines = [f"{icon} Job *{job.name}*{duration}"]
            if run.stdout:
                lines.append(f"```\n{run.stdout.strip()[:500]}\n```")
            if run.stderr:
                lines.append(f"⚠️ stderr: {run.stderr.strip()[:200]}")
            if run.error:
                lines.append(f"Error: {run.error}")

            # Webhook delivery
            if deliver and getattr(deliver, "mode", None) == "webhook":
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

            try:
                await self._telegram_bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                )
            except Exception as e:
                self._logger.error(f"Job notify send failed: {e}")

        self._job_scheduler = JobScheduler(
            self.config.jobs,
            agent_executor=_agent_executor,
            notify_callback=_job_notify,
        )
        await self._job_scheduler.start()
        self._logger.info("Job scheduler started")

    async def _init_todos(self) -> None:
        """Initialize the TODO store."""
        from pyclaw.todos.store import TodoStore
        persist = self.config.todos.persist_file if hasattr(self.config, "todos") else "~/.pyclaw/todos.json"
        self._todo_store = TodoStore(persist_path=persist)
        self._logger.info(f"TODO store initialised ({persist})")

    async def _init_pulse(self) -> None:
        """Initialize pulse runner for heartbeats."""
        self._logger.info("Initializing pulse runner")

        # Create agent executor
        async def pulse_executor(agent_id: str, prompt: str) -> str:
            """Execute pulse task - run agent with prompt, send to Telegram."""
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%H:%M:%S")
            self._logger.info(f"🫀 Pulse tick [{ts}] agent={agent_id}")

            result = "🫀 Pulse"
            try:
                agent = self._agent_manager.get_agent(agent_id)
                if agent:
                    agent_result = await agent.run_heartbeat(prompt)
                    if agent_result and not agent_result.startswith("I hit an internal error"):
                        from pyclaw.agents.runner import strip_thinking_tags
                        result = strip_thinking_tags(agent_result)
                    else:
                        self._logger.warning(f"Agent heartbeat failed: {agent_result}")
                else:
                    self._logger.warning(f"No agent found for pulse: {agent_id}")
            except Exception as e:
                self._logger.error(f"Pulse agent error: {e}")

            # Always store for TUI display
            self._last_pulse_result = result

            # Always send to Telegram via the bot configured for this agent
            _pulse_bot, _pulse_chat_id = self._bot_and_chat_for_agent(agent_id)
            if _pulse_bot and _pulse_chat_id:
                try:
                    await _pulse_bot.send_message(
                        chat_id=_pulse_chat_id,
                        text=f"🫀 [{ts}] {result}",
                    )
                    self._logger.info(f"Pulse sent to Telegram (agent={agent_id}): {result[:80]}")
                except Exception as te:
                    self._logger.error(f"Telegram pulse send failed (agent={agent_id}): {te}")

            # Send to Slack if configured
            slack_cfg = self.config.channels.slack if self.config.channels else None
            if self._slack_web_client and slack_cfg and slack_cfg.pulse_channel:
                try:
                    await self._slack_web_client.chat_postMessage(
                        channel=slack_cfg.pulse_channel,
                        text=f"🫀 [{ts}] {result}",
                    )
                    self._logger.info(f"Pulse sent to Slack #{slack_cfg.pulse_channel}: {result[:80]}")
                except Exception as se:
                    self._logger.error(f"Slack pulse send failed: {se}")

            return result

        # Create pulse runner with executor
        self._pulse_runner = PulseRunner(agent_executor=pulse_executor)

        # Register pulse tasks from agent heartbeat configs
        for agent_id, agent_config_dict in self.config.agents.model_dump().items():
            heartbeat_config = agent_config_dict.get("heartbeat", {})
            if heartbeat_config.get("enabled", False):
                # Parse interval (e.g., "5m" -> 300 seconds)
                interval_str = heartbeat_config.get("every", "30m")
                interval_seconds = self._parse_interval(interval_str)

                # Create active hours if specified
                active_hours_config = heartbeat_config.get("activeHours")
                active_hours = None
                if active_hours_config:
                    active_hours = PulseActiveHours(
                        start=active_hours_config.get("start", "00:00"),
                        end=active_hours_config.get("end", "23:59"),
                    )

                pulse_task = PulseTask(
                    agent_id=agent_id,
                    interval_seconds=interval_seconds,
                    prompt=heartbeat_config.get("prompt", "Check for updates."),
                    active_hours=active_hours,
                    enabled=True,
                )
                self._pulse_runner.register_task(pulse_task)
                self._logger.info(
                    f"Registered pulse task for agent '{agent_id}' every {interval_seconds}s"
                )

        self._logger.info("Pulse runner initialized")

    async def _telegram_poll_bot(self, bot_name: str, bot: Any) -> None:
        """Long-poll one Telegram bot for incoming messages and dispatch them."""
        offset: Optional[int] = None
        self._logger.info(f"Telegram polling loop running (bot={bot_name})")
        while self._is_running:
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                for update in updates:
                    offset = update.update_id + 1
                    if update.message and update.message.text:
                        asyncio.create_task(
                            self._handle_telegram_message(update.message, bot_name=bot_name, bot=bot)
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Telegram poll error (bot={bot_name}): {e}")
                await asyncio.sleep(5)
        self._logger.info(f"Telegram polling loop stopped (bot={bot_name})")

    async def _telegram_poll(self) -> None:
        """Compat shim: poll the first bot (single-bot mode)."""
        if self._tg_bots:
            bot_name, bot = next(iter(self._tg_bots.items()))
            await self._telegram_poll_bot(bot_name, bot)

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

    async def _handle_telegram_message(
        self,
        message: Any,
        bot_name: str = "_default",
        bot: Optional[Any] = None,
    ) -> None:
        """Route one incoming Telegram message to the agent and reply.

        Parameters
        ----------
        message:
            Telegram Message object from python-telegram-bot.
        bot_name:
            Name of the bot that received this message (used for per-bot
            access-control resolution, agent routing, and dedup keying).
        bot:
            The Bot instance to use for replies.  Falls back to the first
            registered bot when omitted (single-bot / compat mode).
        """
        bot = bot or self._telegram_bot
        user_id = str(message.from_user.id)
        chat_id = str(message.chat.id)
        text = message.text or ""

        self._logger.info(
            f"Telegram message received: bot={bot_name} user={user_id} chat={chat_id} "
            f"msg_id={message.message_id} text={text[:60]!r}"
        )

        # Dedup: include bot_name so the same message_id on different bots
        # is not incorrectly treated as a duplicate.
        if self._is_duplicate_message(f"telegram/{bot_name}", str(message.message_id)):
            self._logger.debug(f"Dropping duplicate Telegram message_id={message.message_id} (bot={bot_name})")
            return

        # Resolve effective per-bot access-control lists
        telegram_config = self.config.channels.telegram
        uid_int = int(user_id)

        # Global denylist always wins
        global_denied = self.config.security.denied_users
        if global_denied and uid_int in global_denied:
            self._logger.debug(f"Blocked globally denied user {user_id}")
            return

        # Resolve per-bot config (falls back to parent TelegramConfig for single-bot mode)
        if telegram_config and telegram_config.bots and bot_name in telegram_config.bots:
            effective_tg = telegram_config.effective_config_for_bot(bot_name)
        else:
            effective_tg = telegram_config

        channel_denied = effective_tg.denied_users if effective_tg else []
        if channel_denied and uid_int in channel_denied:
            self._logger.debug(f"Blocked channel-denied user {user_id} (bot={bot_name})")
            return

        channel_allowed = effective_tg.allowed_users if effective_tg else []
        global_allowed = self.config.security.allowed_users if hasattr(self.config.security, "allowed_users") else []
        effective_allowed = channel_allowed if channel_allowed else global_allowed
        if effective_allowed and uid_int not in effective_allowed:
            self._logger.debug(f"Ignored Telegram message from unauthorized user {user_id} (bot={bot_name})")
            return

        sender_name = getattr(message.from_user, "first_name", None) or user_id
        self._logger.info(
            f"Telegram incoming from {sender_name} ({user_id}) via bot={bot_name}: {text[:60]}"
        )

        # Resolve which agent handles this bot's messages
        agent_id = self._agent_id_for_bot(bot_name)

        # Intercept slash commands before routing to the agent
        if text.strip().startswith("/"):
            session = await self._get_or_create_session(
                agent_id=agent_id,
                channel="telegram",
                user_id=user_id,
            )
            # Fire command hook before dispatch
            cmd_name = text.strip().split()[0][1:].lower()
            await self._fire(f"command:{cmd_name}", {
                "command": cmd_name,
                "args": text.strip().split()[1:],
                "session_id": session.id if session else None,
                "channel": "telegram",
                "sender_id": user_id,
            })
            from pyclaw.core.commands import CommandContext
            ctx = CommandContext(
                gateway=self,
                session=session,
                sender_id=user_id,
                channel="telegram",
            )
            reply = await self._command_registry.dispatch(text.strip(), ctx)
            if reply is not None:
                try:
                    await bot.send_message(chat_id=chat_id, text=reply)
                except Exception as e:
                    self._logger.error(f"Failed to send command reply: {e}")
                return

        # Resolve per-bot streaming flag
        streaming = getattr(effective_tg, "streaming", False) if effective_tg else False

        # If streaming is enabled, hand off to the streaming path immediately
        if streaming:
            await self._stream_telegram_response(
                chat_id=chat_id,
                user_id=user_id,
                sender_name=sender_name,
                text=text,
                message_id=str(message.message_id),
                bot_name=bot_name,
                bot=bot,
            )
            return

        # Resolve per-bot typing indicator flag
        typing_indicator = getattr(effective_tg, "typing_indicator", True) if effective_tg else True

        typing_task: Optional[asyncio.Task] = None
        if typing_indicator:
            # Fire immediately so the user sees the indicator before the agent begins
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

            async def _keep_typing():
                """Continue refreshing the typing indicator every 4 s."""
                while True:
                    await asyncio.sleep(4)  # Telegram typing lasts ~5s; refresh before it expires
                    try:
                        await bot.send_chat_action(chat_id=chat_id, action="typing")
                    except Exception:
                        pass

            typing_task = asyncio.create_task(_keep_typing())

        try:
            task = asyncio.create_task(self.handle_message(
                channel="telegram",
                sender=sender_name,
                sender_id=user_id,
                content=text,
                message_id=str(message.message_id),
                agent_id=agent_id,
            ))
            # Track task by session key so /stop can cancel it
            session_key = f"telegram:{user_id}"
            self._active_tasks[session_key] = task
            try:
                response = await task
            finally:
                self._active_tasks.pop(session_key, None)
            if response:
                _agent = self._agent_manager.get_agent(agent_id) if self._agent_manager else None
                _show_thinking = getattr(getattr(_agent, "config", None), "show_thinking", False)
                if _show_thinking:
                    from pyclaw.agents.runner import format_thinking_for_telegram
                    combined = format_thinking_for_telegram(response)
                    if combined:
                        # Thinking found — send as single HTML message (spoiler + response)
                        for chunk in self._split_message(combined):
                            await bot.send_message(
                                chat_id=chat_id,
                                text=chunk,
                                parse_mode="HTML",
                            )
                    else:
                        # No thinking blocks — send response as plain text
                        for chunk in self._split_message(response):
                            await bot.send_message(chat_id=chat_id, text=chunk)
                else:
                    for chunk in self._split_message(response):
                        await bot.send_message(chat_id=chat_id, text=chunk)
        except asyncio.CancelledError:
            self._logger.info(f"Telegram message cancelled for {user_id}")
        except Exception as e:
            self._logger.error(
                f"Error handling Telegram message from {user_id} (bot={bot_name}): {e}"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=f"Sorry, I hit an error: {e}")
            except Exception:
                pass
        finally:
            if typing_task and not typing_task.done():
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    async def _stream_telegram_response(
        self,
        chat_id: str,
        user_id: str,
        sender_name: str,
        text: str,
        message_id: str,
        bot_name: str = "_default",
        bot: Optional[Any] = None,
    ) -> None:
        """Stream an agent response to Telegram by editing a single message in place.

        Sends the first message as soon as any content arrives, then edits it
        every ~1 s as new chunks arrive.  On completion the message is replaced
        with the final formatted text (thinking blockquote if enabled).
        """
        import time
        import html as _html

        bot = bot or self._telegram_bot
        THROTTLE_S = 0.5   # minimum seconds between edits

        # ── session / agent setup ─────────────────────────────────────────
        agent_id = self._agent_id_for_bot(bot_name)
        session = await self._get_or_create_session(
            agent_id=agent_id, channel="telegram", user_id=user_id
        )
        if session is None:
            await bot.send_message(chat_id=chat_id, text="Could not create session.")
            return

        agent = self._agent_manager.get_agent(agent_id)
        if agent is None:
            await bot.send_message(chat_id=chat_id, text="No agent available.")
            return

        show_thinking = getattr(getattr(agent, "config", None), "show_thinking", False)
        model_override = session.context.get("model_override")
        runner = agent._get_session_runner(
            session.id,
            model_override=model_override,
            history_path=session.history_path,
        )

        # Send typing indicator immediately so the user sees activity
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

        # Keep the typing indicator alive during long thinking phases.
        # Telegram's "typing" action expires after ~5 s; refresh it every 4 s
        # until we have sent the first real message chunk.
        typing_active = True

        async def _keep_typing_stream():
            while typing_active:
                await asyncio.sleep(4)
                if not typing_active:
                    break
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass

        typing_task = asyncio.create_task(_keep_typing_stream())

        # ── mutable stream state ──────────────────────────────────────────
        stream_msg_id: Optional[int] = None
        last_edit_time: float = 0.0
        # Separate buffers: thinking_buffer has is_reasoning=True chunks (no tags),
        # response_buffer has is_reasoning=False chunks (may have stray <think> tags
        # from providers that leak thinking into delta.content).
        thinking_buffer: str = ""
        response_buffer: str = ""

        from pyclaw.agents.runner import strip_thinking_tags
        import re as _re
        _OPEN_THINK = _re.compile(r"<(thinking|think)>", _re.IGNORECASE)

        def _live_display(buf: str) -> str:
            """Return text safe to show mid-stream.

            Strips complete <think>…</think> blocks, then hides everything
            from any still-open <think> tag to the end of the buffer (so
            partial thinking blocks never flash onscreen).
            """
            stripped = strip_thinking_tags(buf)
            # If an opening tag remains, the block isn't closed yet — hide it
            m = _OPEN_THINK.search(stripped)
            if m:
                return stripped[: m.start()].strip()
            return stripped

        # ── stream loop ───────────────────────────────────────────────────
        session_key = f"telegram:{user_id}"

        async def _run_stream() -> None:
            nonlocal stream_msg_id, last_edit_time, thinking_buffer, response_buffer, typing_active

            async for chunk_text, is_reasoning in runner.run_stream(text):
                if is_reasoning:
                    thinking_buffer += chunk_text
                else:
                    response_buffer += chunk_text

                # Build a single unified HTML display — blockquote for thinking,
                # plain text after it for the response.  Every edit sends a
                # complete, valid HTML string so there's no phase transition.
                if show_thinking and thinking_buffer:
                    tail = thinking_buffer[-600:] if len(thinking_buffer) > 600 else thinking_buffer
                    safe_t = _html.escape(tail, quote=False)
                    display = f"<blockquote expandable><i>💭 {safe_t}</i></blockquote>"
                    response_part = _live_display(response_buffer)
                    if response_part:
                        display += f"\n\n{_html.escape(response_part, quote=False)}"
                    mid_parse_mode: Optional[str] = "HTML"
                else:
                    display = _live_display(response_buffer)
                    if not display:
                        continue
                    mid_parse_mode = None

                now = time.monotonic()
                if stream_msg_id is None:
                    typing_active = False  # stop refreshing typing indicator
                    try:
                        msg = await bot.send_message(
                            chat_id=chat_id, text=display,
                            **({"parse_mode": mid_parse_mode} if mid_parse_mode else {}),
                        )
                        stream_msg_id = msg.message_id
                        last_edit_time = now
                    except Exception as _se:
                        self._logger.warning(f"Stream: initial send failed: {_se}")
                elif now - last_edit_time >= THROTTLE_S:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=stream_msg_id,
                            text=display,
                            **({"parse_mode": mid_parse_mode} if mid_parse_mode else {}),
                        )
                        last_edit_time = now
                    except Exception:
                        pass  # "message is not modified" etc. are harmless

            # ── final edit ────────────────────────────────────────────────
            clean_response = strip_thinking_tags(response_buffer)
            if show_thinking and thinking_buffer:
                # Proper thinking came via is_reasoning=True — format as spoiler
                safe_thinking = _html.escape(thinking_buffer.strip(), quote=False)
                safe_response = _html.escape(clean_response, quote=False)
                final_text = f"<blockquote expandable><i>💭 {safe_thinking}</i></blockquote>\n\n{safe_response}"
                parse_mode = "HTML"
            elif show_thinking:
                # No separated thinking — try to extract <think> tags from response_buffer
                from pyclaw.agents.runner import format_thinking_for_telegram
                combined = format_thinking_for_telegram(response_buffer)
                if combined:
                    final_text = combined
                    parse_mode = "HTML"
                else:
                    final_text = clean_response
                    parse_mode = None
            else:
                final_text = clean_response
                parse_mode = None

            if not final_text:
                return

            send_kwargs: Dict[str, Any] = {"text": final_text}
            if parse_mode:
                send_kwargs["parse_mode"] = parse_mode

            try:
                if stream_msg_id is not None:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=stream_msg_id, **send_kwargs
                    )
                else:
                    await bot.send_message(chat_id=chat_id, **send_kwargs)
            except Exception as fe:
                fe_str = str(fe).lower()
                if "message is not modified" in fe_str:
                    pass  # content already matches — not an error
                else:
                    self._logger.error(f"Stream: final edit failed: {fe}")
                    # Only fall back to a fresh message for genuine failures
                    try:
                        await bot.send_message(chat_id=chat_id, **send_kwargs)
                    except Exception:
                        pass

            # Debug: send raw buffers so we can see exactly what came from the model
            if getattr(getattr(self, "config", None), "gateway", None) and self.config.gateway.debug:
                try:
                    debug_text = f"🔍 thinking_buffer:\n{thinking_buffer[:1000]}\n\n📝 response_buffer:\n{response_buffer[:1000]}"
                    await bot.send_message(chat_id=chat_id, text=debug_text[:2000])
                except Exception:
                    pass

            # Update session activity
            session.touch(count_delta=2)

            # Usage counters
            self._usage["messages_total"] += 1
            self._usage["messages_by_channel"]["telegram"] = (
                self._usage["messages_by_channel"].get("telegram", 0) + 1
            )

        task = asyncio.create_task(_run_stream())
        self._active_tasks[session_key] = task
        try:
            await task
        except asyncio.CancelledError:
            self._logger.info(f"Telegram stream cancelled for {user_id}")
        except Exception as e:
            import traceback
            self._logger.error(f"Telegram stream error for {user_id} (bot={bot_name}): {e}\n{traceback.format_exc()}")
            # Evict the broken session runner so the next attempt gets a fresh one
            _agent = self._agent_manager.get_agent(agent_id) if self._agent_manager else None
            try:
                if _agent and session:
                    await _agent.evict_session_runner(session.id)
            except Exception:
                pass
            # If the task group expired (FastAgent lifecycle race on startup), retry once
            # silently with a fresh runner rather than surfacing the error to the user.
            if "task group" in str(e).lower() and _agent and session:
                self._logger.info(
                    f"Task group expired for {agent_id}/{session.id[:8]} — retrying with fresh runner"
                )
                try:
                    fresh_runner = _agent._get_session_runner(
                        session.id,
                        model_override=model_override,
                        history_path=session.history_path,
                    )
                    # Reset stream state for retry
                    thinking_buffer = ""
                    response_buffer = ""
                    stream_msg_id = None
                    # Re-run the stream with the fresh runner — reuse _run_stream closure
                    # by pointing runner at the new instance and re-executing
                    # (simplest: call the non-streaming path as fallback)
                    retry_result = await fresh_runner.run(text)
                    if retry_result:
                        from pyclaw.agents.runner import strip_thinking_tags
                        await bot.send_message(chat_id=chat_id, text=strip_thinking_tags(retry_result))
                except Exception as retry_err:
                    self._logger.error(f"Retry also failed for {user_id}: {retry_err}")
                    try:
                        await bot.send_message(chat_id=chat_id, text=f"Sorry, I hit an error: {retry_err}")
                    except Exception:
                        pass
            else:
                try:
                    await bot.send_message(chat_id=chat_id, text=f"Sorry, I hit an error: {e}")
                except Exception:
                    pass
        finally:
            typing_active = False
            if not typing_task.done():
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
            self._active_tasks.pop(session_key, None)

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

        # Get agent + session
        agent_id = (
            next(iter(self._agent_manager.agents))
            if self._agent_manager and self._agent_manager.agents
            else "default"
        )
        session = await self._get_or_create_session(
            agent_id=agent_id,
            channel="slack",
            user_id=session_key,
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
            from pyclaw.core.commands import CommandContext
            ctx = CommandContext(
                gateway=self,
                session=session,
                sender_id=user_id,
                channel="slack",
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
        response = await self.handle_message(
            channel="slack",
            sender=user_id,
            sender_id=user_id,
            content=text,
            message_id=ts,
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
            from pyclaw.jobs.models import Job, CommandRun, CronSchedule, DeliverAnnounce
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

    @staticmethod
    def _parse_interval(interval_str: str) -> int:
        """Parse interval string like '30m', '1h', '5s' to seconds."""
        interval_str = interval_str.lower().strip()

        if interval_str.endswith("s"):
            return int(interval_str[:-1])
        elif interval_str.endswith("m"):
            return int(interval_str[:-1]) * 60
        elif interval_str.endswith("h"):
            return int(interval_str[:-1]) * 3600
        elif interval_str.endswith("d"):
            return int(interval_str[:-1]) * 86400
        else:
            return int(interval_str)

    async def start(self) -> None:
        """Start the gateway."""
        if self._is_running:
            self._logger.warning("Gateway already running")
            return

        await self.initialize()

        # Start pulse runner
        if self._pulse_runner:
            await self._pulse_runner.start()
            self._logger.info("Pulse runner started")

        self._is_running = True
        self._logger.info("pyclaw Gateway started")

        # Start Telegram long-polling — one task per configured bot
        for bot_name, bot in self._tg_bots.items():
            task = asyncio.create_task(
                self._telegram_poll_bot(bot_name, bot),
                name=f"telegram-poll-{bot_name}",
            )
            self._tg_polling_tasks[bot_name] = task
        if self._tg_bots:
            self._logger.info(
                f"Telegram polling started for {len(self._tg_bots)} bot(s): {list(self._tg_bots)}"
            )

        # Keep running
        try:
            while self._is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self._logger.info("Gateway cancelled")

    async def start_mcp_server(self, host: str = "0.0.0.0", port: int = 8081) -> None:
        """Start the pyclaw MCP HTTP server as a managed background task.

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

        from pyclaw.tools.server import mcp as pyclaw_mcp

        async def _run():
            try:
                await pyclaw_mcp.run_http_async(host=host, port=port, show_banner=False)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._logger.error(f"MCP server error: {e}")

        self._mcp_server_task = asyncio.create_task(_run(), name="pyclaw-mcp-server")
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
        from pyclaw.api.app import create_app

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

        self._api_server_task = asyncio.create_task(_run(), name="pyclaw-api-server")
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
        self._logger.info("Stopping pyclaw Gateway...")
        await self._fire(HookEvent.GATEWAY_SHUTDOWN, {})

        self._is_running = False

        # Stop Telegram polling (all bots)
        for bot_name, task in list(self._tg_polling_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tg_polling_tasks.clear()

        # Stop agents
        if self._agent_manager:
            await self.agent_manager.stop_all()

        # Stop session manager
        if self._session_manager:
            await self.session_manager.stop()

        # Stop job scheduler
        if self._job_scheduler:
            await self.job_scheduler.stop()

        # Stop pulse runner
        if self._pulse_runner:
            await self._pulse_runner.stop()

        # Stop channel plugins
        for name, channel in list(self._channels.items()):
            try:
                await channel.stop()
                self._logger.debug(f"Channel plugin '{name}' stopped")
            except Exception as exc:
                self._logger.warning(f"Channel plugin '{name}' stop error: {exc}")

        # Stop MCP and API servers
        await self.stop_mcp_server()
        await self.stop_api_server()

        self._logger.info("pyclaw Gateway stopped")

    async def _get_or_create_session(
        self,
        agent_id: str,
        channel: str,
        user_id: str,
    ) -> Optional[Any]:
        """Wrapper around SessionManager.get_or_create_session that fires session:created."""
        session = await self.session_manager.get_or_create_session(
            agent_id=agent_id, channel=channel, user_id=user_id
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

    async def handle_message(
        self,
        channel: str,
        sender: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Handle an incoming message."""
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

        # Get or create session (fires session:created for new sessions)
        session = await self._get_or_create_session(
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

        # Get agent
        agent = self.agent_manager.get_agent(session.agent_id)
        if agent is None:
            return "No agent available"

        # Handle message
        response = await agent.handle_message(message, session)
        response_text = response.content if response else None

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
            _logging.getLogger("pyclaw").setLevel(level)
            changed["gateway.log_level"] = {"old": old_log, "new": new_log}

        # Concurrency limits
        old_cc = old_config.concurrency
        new_cc = new_cfg.concurrency
        if old_cc.default != new_cc.default or old_cc.models != new_cc.models:
            from pyclaw.core.concurrency import init_manager
            init_manager(model_limits=new_cc.models, default=new_cc.default)
            changed["concurrency"] = {
                "old": {"default": old_cc.default, "models": old_cc.models},
                "new": {"default": new_cc.default, "models": new_cc.models},
            }

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
                    from pyclaw.config.schema import AgentConfig
                    managed.config = AgentConfig(**new_agent_dict)
                    # Recreate the base runner with the new config
                    from pyclaw.agents.runner import AgentRunner
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

    async def run_heartbeats(self) -> None:
        """Run heartbeat checks for all agents."""
        for agent in self.agent_manager.list_agents():
            if agent.config.heartbeat.enabled:
                try:
                    result = await agent.run_heartbeat(agent.config.heartbeat.prompt)
                    if result:
                        self._logger.debug(f"Heartbeat result for {agent.name}: {result}")
                except Exception as e:
                    self._logger.error(f"Heartbeat error for {agent.name}: {e}")

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
