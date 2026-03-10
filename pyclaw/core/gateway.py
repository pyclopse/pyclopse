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
        self._telegram_bot: Optional[Any] = None
        self._telegram_chat_id: Optional[str] = None
        self._telegram_polling_task: Optional[asyncio.Task] = None
        self._slack_web_client: Optional[Any] = None  # AsyncWebClient for outbound Slack messages
        # Active processing tasks keyed by session ID (used by /stop)
        self._active_tasks: Dict[str, asyncio.Task] = {}

        # Inbound dedup cache: "channel:message_id" → timestamp of first processing
        self._seen_message_ids: Dict[str, float] = {}
        self._dedup_ttl_seconds: int = 60

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

    @property
    def telegram_bot(self) -> Optional[Any]:
        """Get Telegram bot instance."""
        return self._telegram_bot

    def set_telegram_target(self, chat_id: str) -> None:
        """Set the Telegram chat ID for pulse messages."""
        self._telegram_chat_id = chat_id

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

        # Initialize Telegram bot if configured
        telegram_config = self.config.channels.telegram
        self._logger.info(f"Telegram config: enabled={telegram_config.enabled if telegram_config else None}, bot_token={'set' if telegram_config and telegram_config.bot_token else 'empty'}")
        if telegram_config and telegram_config.enabled and telegram_config.bot_token:
            try:
                from telegram import Bot

                self._telegram_bot = Bot(token=telegram_config.bot_token)
                # Get bot info
                me = await self._telegram_bot.get_me()
                self._logger.info(f"Telegram bot initialized: @{me.username}")

                # Register slash commands with Telegram so they appear in the UI picker
                await self._register_telegram_commands()

                # If allowed_users is set, use the first one as target for now
                if telegram_config.allowed_users:
                    self._telegram_chat_id = str(telegram_config.allowed_users[0])
                    self._logger.info(f"Telegram pulse target chat_id: {self._telegram_chat_id}")
            except ImportError:
                self._logger.warning("python-telegram-bot not installed, Telegram disabled")
            except Exception as e:
                self._logger.error(f"Failed to initialize Telegram bot: {e}")
        else:
            self._logger.info("Channel adapters not yet implemented")

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

    async def _register_telegram_commands(self) -> None:
        """Register slash commands with Telegram so they appear in the UI command picker."""
        if not self._telegram_bot:
            return
        try:
            from telegram import BotCommand
            commands = [
                BotCommand(cmd, desc)
                for cmd, desc in self._command_registry.commands_for_telegram()
            ]
            await self._telegram_bot.set_my_commands(commands)
            self._logger.info(f"Registered {len(commands)} Telegram commands")
        except Exception as e:
            self._logger.warning(f"Failed to register Telegram commands: {e}")

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

            # Always send to Telegram (heartbeat proof-of-life)
            if self._telegram_bot and self._telegram_chat_id:
                try:
                    await self._telegram_bot.send_message(
                        chat_id=self._telegram_chat_id,
                        text=f"🫀 [{ts}] {result}",
                    )
                    self._logger.info(f"Pulse sent to Telegram: {result[:80]}")
                except Exception as te:
                    self._logger.error(f"Telegram send failed: {te}")

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

    async def _telegram_poll(self) -> None:
        """Long-poll Telegram for incoming messages and dispatch them."""
        offset: Optional[int] = None
        self._logger.info("Telegram polling loop running")
        while self._is_running:
            try:
                updates = await self._telegram_bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                for update in updates:
                    offset = update.update_id + 1
                    if update.message and update.message.text:
                        asyncio.create_task(
                            self._handle_telegram_message(update.message)
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(5)
        self._logger.info("Telegram polling loop stopped")

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

    async def _handle_telegram_message(self, message: Any) -> None:
        """Route one incoming Telegram message to the agent and reply."""
        user_id = str(message.from_user.id)
        chat_id = str(message.chat.id)
        text = message.text or ""

        # Dedup: skip re-delivered updates
        if self._is_duplicate_message("telegram", str(message.message_id)):
            self._logger.debug(f"Dropping duplicate Telegram message_id={message.message_id}")
            return

        # Enforce access control — per-channel lists override the global security config
        telegram_config = self.config.channels.telegram
        uid_int = int(user_id)

        # Global denylist always wins
        global_denied = self.config.security.denied_users
        if global_denied and uid_int in global_denied:
            self._logger.debug(f"Blocked globally denied user {user_id}")
            return

        # Channel-level denied_users
        channel_denied = telegram_config.denied_users if telegram_config else []
        if channel_denied and uid_int in channel_denied:
            self._logger.debug(f"Blocked channel-denied user {user_id}")
            return

        # Channel-level allowed_users takes precedence over global allowed_users
        channel_allowed = telegram_config.allowed_users if telegram_config else []
        global_allowed = self.config.security.allowed_users if hasattr(self.config.security, "allowed_users") else []
        effective_allowed = channel_allowed if channel_allowed else global_allowed
        if effective_allowed and uid_int not in effective_allowed:
            self._logger.debug(f"Ignored Telegram message from unauthorized user {user_id}")
            return

        sender_name = getattr(message.from_user, "first_name", None) or user_id
        self._logger.info(
            f"Telegram incoming from {sender_name} ({user_id}): {text[:60]}"
        )

        # Intercept slash commands before routing to the agent
        if text.strip().startswith("/"):
            agent_id = (
                next(iter(self._agent_manager.agents))
                if self._agent_manager and self._agent_manager.agents
                else "default"
            )
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
                    await self._telegram_bot.send_message(chat_id=chat_id, text=reply)
                except Exception as e:
                    self._logger.error(f"Failed to send command reply: {e}")
                return

        # If streaming is enabled, hand off to the streaming path immediately
        if telegram_config and getattr(telegram_config, "streaming", False):
            await self._stream_telegram_response(
                chat_id=chat_id,
                user_id=user_id,
                sender_name=sender_name,
                text=text,
                message_id=str(message.message_id),
            )
            return

        # Send typing indicator while agent processes (if enabled)
        typing_indicator = (
            self.config.channels.telegram.typing_indicator
            if self.config.channels.telegram
            else True
        )
        typing_task: Optional[asyncio.Task] = None
        if typing_indicator:
            # Fire immediately so the user sees the indicator before the agent begins
            try:
                await self._telegram_bot.send_chat_action(
                    chat_id=chat_id, action="typing"
                )
            except Exception:
                pass

            async def _keep_typing():
                """Continue refreshing the typing indicator every 4 s."""
                while True:
                    await asyncio.sleep(4)  # Telegram typing lasts ~5s; refresh before it expires
                    try:
                        await self._telegram_bot.send_chat_action(
                            chat_id=chat_id, action="typing"
                        )
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
            ))
            # Track task by session key so /stop can cancel it
            session_key = f"telegram:{user_id}"
            self._active_tasks[session_key] = task
            try:
                response = await task
            finally:
                self._active_tasks.pop(session_key, None)
            if response:
                _agent_id = (
                    next(iter(self._agent_manager.agents))
                    if self._agent_manager and self._agent_manager.agents
                    else None
                )
                _agent = self._agent_manager.get_agent(_agent_id) if _agent_id and self._agent_manager else None
                _show_thinking = getattr(getattr(_agent, "config", None), "show_thinking", False)
                if _show_thinking:
                    from pyclaw.agents.runner import format_thinking_for_telegram
                    combined = format_thinking_for_telegram(response)
                    if combined:
                        # Thinking found — send as single HTML message (spoiler + response)
                        for chunk in self._split_message(combined):
                            await self._telegram_bot.send_message(
                                chat_id=chat_id,
                                text=chunk,
                                parse_mode="HTML",
                            )
                    else:
                        # No thinking blocks — send response as plain text
                        for chunk in self._split_message(response):
                            await self._telegram_bot.send_message(
                                chat_id=chat_id,
                                text=chunk,
                            )
                else:
                    for chunk in self._split_message(response):
                        await self._telegram_bot.send_message(
                            chat_id=chat_id,
                            text=chunk,
                        )
        except asyncio.CancelledError:
            self._logger.info(f"Telegram message cancelled for {user_id}")
        except Exception as e:
            self._logger.error(
                f"Error handling Telegram message from {user_id}: {e}"
            )
            try:
                await self._telegram_bot.send_message(
                    chat_id=chat_id,
                    text=f"Sorry, I hit an error: {e}",
                )
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
    ) -> None:
        """Stream an agent response to Telegram by editing a single message in place.

        Sends the first message as soon as any content arrives, then edits it
        every ~1 s as new chunks arrive.  On completion the message is replaced
        with the final formatted text (thinking blockquote if enabled).
        """
        import time
        import html as _html

        THROTTLE_S = 1.0   # minimum seconds between edits

        # ── session / agent setup ─────────────────────────────────────────
        agent_id = (
            next(iter(self._agent_manager.agents))
            if self._agent_manager and self._agent_manager.agents
            else "default"
        )
        session = await self._get_or_create_session(
            agent_id=agent_id, channel="telegram", user_id=user_id
        )
        if session is None:
            await self._telegram_bot.send_message(
                chat_id=chat_id, text="Could not create session."
            )
            return

        agent = self._agent_manager.get_agent(agent_id)
        if agent is None:
            await self._telegram_bot.send_message(
                chat_id=chat_id, text="No agent available."
            )
            return

        show_thinking = getattr(getattr(agent, "config", None), "show_thinking", False)
        model_override = session.context.get("model_override")
        runner = agent._get_session_runner(session.id, model_override=model_override)

        # ── mutable stream state ──────────────────────────────────────────
        stream_msg_id: Optional[int] = None
        last_edit_time: float = 0.0
        # raw accumulator — may contain <think> tags when is_reasoning is unused
        raw_buffer: str = ""

        from pyclaw.agents.runner import strip_thinking_tags, format_thinking_for_telegram
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
            nonlocal stream_msg_id, last_edit_time, raw_buffer

            async for chunk_text, is_reasoning in runner.run_stream(text):
                if is_reasoning:
                    # Provider gave us a clean reasoning chunk — skip display
                    raw_buffer += chunk_text  # keep for final show_thinking formatting
                    continue

                raw_buffer += chunk_text
                now = time.monotonic()

                display = _live_display(raw_buffer)
                if not display:
                    continue

                if stream_msg_id is None:
                    try:
                        msg = await self._telegram_bot.send_message(
                            chat_id=chat_id, text=display
                        )
                        stream_msg_id = msg.message_id
                        last_edit_time = now
                    except Exception as _se:
                        self._logger.warning(f"Stream: initial send failed: {_se}")
                elif now - last_edit_time >= THROTTLE_S:
                    try:
                        await self._telegram_bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=stream_msg_id,
                            text=display,
                        )
                        last_edit_time = now
                    except Exception:
                        pass  # "message is not modified" etc. are harmless

            # ── final edit ────────────────────────────────────────────────
            self._logger.debug(
                f"Stream final raw_buffer (len={len(raw_buffer)}):\n{repr(raw_buffer[:500])}"
            )
            if show_thinking:
                combined = format_thinking_for_telegram(raw_buffer)
                if combined:
                    final_text = combined
                    parse_mode = "HTML"
                else:
                    final_text = strip_thinking_tags(raw_buffer)
                    parse_mode = None
            else:
                final_text = strip_thinking_tags(raw_buffer)
                parse_mode = None

            if not final_text:
                return

            send_kwargs: Dict[str, Any] = {"text": final_text}
            if parse_mode:
                send_kwargs["parse_mode"] = parse_mode

            try:
                if stream_msg_id is not None:
                    await self._telegram_bot.edit_message_text(
                        chat_id=chat_id, message_id=stream_msg_id, **send_kwargs
                    )
                else:
                    await self._telegram_bot.send_message(
                        chat_id=chat_id, **send_kwargs
                    )
            except Exception as fe:
                self._logger.error(f"Stream: final edit failed: {fe}")
                # Fall back to a fresh message
                try:
                    await self._telegram_bot.send_message(
                        chat_id=chat_id, **send_kwargs
                    )
                except Exception:
                    pass

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
            self._logger.error(f"Telegram stream error for {user_id}: {e}")
            try:
                await self._telegram_bot.send_message(
                    chat_id=chat_id, text=f"Sorry, I hit an error: {e}"
                )
            except Exception:
                pass
        finally:
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

        # Start Telegram long-polling if bot is available
        if self._telegram_bot:
            self._telegram_polling_task = asyncio.create_task(self._telegram_poll())
            self._logger.info("Telegram incoming polling started")

        # Keep running
        try:
            while self._is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self._logger.info("Gateway cancelled")

    async def stop(self) -> None:
        """Stop the gateway."""
        self._logger.info("Stopping pyclaw Gateway...")
        await self._fire(HookEvent.GATEWAY_SHUTDOWN, {})

        self._is_running = False

        # Stop Telegram polling
        if self._telegram_polling_task and not self._telegram_polling_task.done():
            self._telegram_polling_task.cancel()
            try:
                await self._telegram_polling_task
            except asyncio.CancelledError:
                pass

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

        # Use first configured agent
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
