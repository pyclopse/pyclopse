"""Slash command registry for pyclopse gateway."""

import logging
from pyclopse.reflect import reflect_system
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("pyclopse.commands")


@dataclass
class CommandContext:
    """Context passed to every command handler.

    Provides the handler with access to the gateway and the current session
    so it can read/modify state without needing direct imports.

    Attributes:
        gateway (Any): The live Gateway instance.
        session (Optional[Any]): The active Session, or None for stateless
            commands (e.g. commands that run before a session is established).
        sender_id (str): Stable user identifier for the caller.
        channel (str): Channel name the command arrived on.
        thread_id (Optional[str]): Telegram topic ID or Slack thread_ts,
            if the command was sent inside a thread.
    """

    gateway: Any          # Gateway instance
    session: Optional[Any]  # Session (may be None for stateless commands)
    sender_id: str
    channel: str
    thread_id: Optional[str] = None  # Telegram topic ID or Slack thread_ts


@dataclass
class Command:
    """A registered slash command.

    Attributes:
        name (str): Lowercase command name without the leading slash.
        description (str): Short human-readable description shown in /help.
        usage (str): Usage string shown in help, e.g. ``"/reset"``.
        handler (Callable): Async callable ``(args: str, ctx: CommandContext)
            -> str`` that implements the command.
    """

    name: str
    description: str
    usage: str
    handler: Callable  # async (args: str, ctx: CommandContext) -> str


@reflect_system("commands")
class CommandRegistry:
    """Registry of slash commands available in pyclopse.

    Commands are registered by name (without the leading slash) and dispatched
    when a message starts with ``/``.  Built-in commands are registered via
    register_builtin_commands(); custom commands can be added via register().
    """

    def __init__(self) -> None:
        """Initialize the CommandRegistry with an empty command table."""
        self._commands: Dict[str, Command] = {}

    def register(
        self,
        name: str,
        handler: Callable,
        description: str,
        usage: str = "",
    ) -> None:
        """Register a slash command handler.

        Args:
            name (str): Command name.  The leading ``/`` is stripped and the
                name is lowercased before storage.
            handler (Callable): Async callable ``(args: str, ctx: CommandContext)
                -> str``.
            description (str): Short description shown in /help output.
            usage (str): Usage string (e.g. ``"/model [name]"``).  Defaults to
                ``"/<name>"`` when empty.
        """
        key = name.lstrip("/").lower()
        self._commands[key] = Command(
            name=key,
            description=description,
            usage=usage or f"/{key}",
            handler=handler,
        )

    async def dispatch(self, text: str, ctx: CommandContext) -> Optional[str]:
        """Dispatch a slash command and return the response string.

        Parses the command name from the first word after ``/`` and looks it up
        in the registry.  Unknown commands return None so callers can fall
        through to agent routing.

        Args:
            text (str): Raw message text, expected to start with ``/``.
            ctx (CommandContext): Execution context for the handler.

        Returns:
            Optional[str]: Response string from the handler, an error message
                if the handler raised, or None if text does not start with ``/``
                or the command name is not registered.
        """
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(maxsplit=1)
        name = parts[0].lstrip("/").lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(name)
        if cmd is None:
            # Return None so callers can fall through to agent routing
            return None

        try:
            return await cmd.handler(args, ctx)
        except Exception as e:
            logger.error(f"Command /{name} raised: {e}", exc_info=True)
            return f"Error running /{name}: {e}"

    def help_text(self) -> str:
        """Return a human-readable list of all registered commands.

        Returns:
            str: Newline-separated list of ``  /name — description`` lines,
                or ``"No commands registered."`` if empty.
        """
        if not self._commands:
            return "No commands registered."
        lines = ["Available commands:"]
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            lines.append(f"  /{cmd.name} — {cmd.description}")
        return "\n".join(lines)

    def commands_for_telegram(self) -> List[tuple]:
        """Return (command, description) pairs suitable for Telegram setMyCommands.

        Truncates command names to 32 characters and descriptions to 256
        characters as required by the Telegram Bot API.

        Returns:
            List[tuple]: List of ``(name, description)`` string tuples, sorted
                alphabetically by command name.
        """
        result = []
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            name = cmd.name[:32].lower()
            desc = (cmd.description or cmd.name)[:256] or name
            result.append((name, desc))
        return result


# ---------------------------------------------------------------------------
# Built-in command handlers
# ---------------------------------------------------------------------------


# FA ACP model subcommands — routed through AgentRunner.acp_execute()
_FA_MODEL_SUBCOMMANDS = frozenset({
    "reasoning", "fast", "verbosity", "web_search", "web_fetch",
    "doctor", "aliases", "catalog",
})


def _get_runner(ctx: CommandContext) -> Optional[Any]:
    """Return the already-initialized AgentRunner for this session, or None.

    Looks up the AgentRunner in the agent's _session_runners dict.  Only
    returns a runner when one already exists (i.e. the session has had at
    least one message processed).  Does NOT create a new runner.

    Args:
        ctx (CommandContext): The command execution context containing the
            gateway and session.

    Returns:
        Optional[Any]: The AgentRunner for the session, or None if no runner
            has been created yet or no session/agent is available.
    """
    if ctx.session is None:
        return None
    gw = ctx.gateway
    if not getattr(gw, "_agent_manager", None):
        return None
    agent = gw._agent_manager.get_agent(ctx.session.agent_id)
    if agent is None:
        return None
    return agent._session_runners.get(ctx.session.id)


def register_builtin_commands(registry: CommandRegistry, gateway: Any) -> None:
    """Register all standard gateway commands into a CommandRegistry.

    Defines and registers the following commands: help, reset, status, model,
    job, start, new, stop, compact, whoami, models, think, usage, context,
    reload, restart, config, export, verbose, approve, skills, skill, reboot,
    subagents, queue, tts, history, clear, mcp, cards, card, agent, bash,
    allowlist, reasoning, session, and focus.

    Args:
        registry (CommandRegistry): The registry to add commands to.
        gateway (Any): The Gateway instance, captured in closures so command
            handlers can access subsystems at call time.
    """

    async def cmd_help(args: str, ctx: CommandContext) -> str:
        return registry.help_text()

    async def cmd_reset(args: str, ctx: CommandContext) -> str:
        if ctx.session is None:
            return "No active session to reset."
        agent = ctx.gateway._agent_manager.get_agent(ctx.session.agent_id) if ctx.gateway._agent_manager else None
        # Archive history files so they're kept on disk but won't be reloaded
        if ctx.session.history_dir and ctx.session.history_dir.exists():
            from datetime import datetime as _dt
            import shutil as _shutil
            archive_dir = ctx.session.history_dir / "archived"
            archive_dir.mkdir(parents=True, exist_ok=True)
            from pyclopse.utils.time import now as _now
            stamp = _now().strftime("%Y%m%d_%H%M%S")
            for hist_file in ["history.json", "history_previous.json"]:
                p = ctx.session.history_dir / hist_file
                if p.exists():
                    p.rename(archive_dir / f"{hist_file}.{stamp}")
        # Reset message count and persist
        ctx.session.message_count = 0
        ctx.session.save_metadata()
        # Evict the per-session runner so the next message gets a fresh FastAgent context
        if agent:
            await agent.evict_session_runner(ctx.session.id)
        # Create a fresh session and make it the active one
        sm = ctx.gateway._session_manager
        if sm:
            channel = ctx.session.last_channel or ctx.session.channel
            user_id = ctx.session.last_user_id or ctx.session.user_id
            thread_ts = ctx.session.last_thread_ts
            new_session = await sm.create_session(
                agent_id=ctx.session.agent_id,
                channel=channel,
                user_id=user_id,
            )
            new_session.last_channel = channel
            new_session.last_user_id = user_id
            new_session.last_thread_ts = thread_ts
            new_session.save_metadata()
            sm.set_active_session(ctx.session.agent_id, new_session.id)
        return "✅ Session history cleared."

    async def cmd_status(args: str, ctx: CommandContext) -> str:
        # FA ACP subcommands: /status system, /status auth
        sub = args.strip().lower()
        if sub in ("system", "auth"):
            runner = _get_runner(ctx)
            if runner is None:
                return "No active agent session — send a message first, then retry."
            return await runner.acp_execute("status", args.strip())

        status = ctx.gateway.get_status()
        agents = status.get("agents", {})
        sessions = status.get("sessions", {})
        jobs = status.get("jobs", {})
        lines = [
            "🟢 pyclopse Gateway",
            f"Running: {status.get('is_running', False)}",
            f"Config version: {status.get('config_version', '?')}",
            f"Agents: {agents.get('total_agents', 0)} "
            f"({agents.get('running_agents', 0)} running)",
            f"Sessions: {sessions.get('active_sessions', 0)} active "
            f"/ {sessions.get('total_sessions', 0)} total",
            f"Jobs: {jobs.get('total', 0)} total "
            f"/ {jobs.get('running', 0)} running",
        ]

        # Context usage — read from live FA agent history (always current)
        if ctx.session is not None:
            agent = (
                ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
                if getattr(ctx.gateway, "_agent_manager", None) else None
            )
            context_window: Optional[int] = getattr(
                getattr(agent, "config", None), "context_window", None
            )
            # Primary: session.context["_ctx_tokens"] — snapshotted after every agent
            # response by gateway._snapshot_ctx_tokens(); survives runner lifecycle.
            ctx_tokens: int = ctx.session.context.get("_ctx_tokens", 0)
            # Secondary: live runner accumulator (more up-to-date mid-conversation)
            runner = _get_runner(ctx)
            if runner is not None and getattr(runner, "_app", None) is not None:
                try:
                    agent_name = getattr(runner, "agent_name", None)
                    fa_agent = runner._app._agent(agent_name)

                    accumulator = getattr(fa_agent, "usage_accumulator", None)
                    history = getattr(fa_agent, "message_history", None) or []
                    live_tokens = 0
                    if accumulator is not None:
                        live_tokens = accumulator.current_context_tokens or 0
                        if context_window is None:
                            context_window = getattr(accumulator, "context_window_size", None)
                    if live_tokens == 0:
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
                        live_tokens = total_chars // 4
                    if live_tokens:
                        ctx_tokens = live_tokens
                except Exception:
                    logger.warning("Failed to read live context tokens from FA", exc_info=True)

            if ctx_tokens:
                if context_window:
                    pct = min(100, int(ctx_tokens * 100 / context_window))
                    bar_filled = pct // 10
                    bar = "█" * bar_filled + "░" * (10 - bar_filled)
                    lines.append(
                        f"Context: {ctx_tokens:,} / {context_window:,} tokens "
                        f"({pct}%) [{bar}]"
                    )
                else:
                    lines.append(f"Context: {ctx_tokens:,} tokens (no limit set)")

        # Provider usage stats (from usage monitors)
        try:
            from pyclopse.core.usage import get_registry
            usage_status = get_registry().status()
            if usage_status:
                lines.append("")
                lines.append("Provider Usage:")
                for provider_name, info in usage_status.items():
                    pct = info.get("usage_pct")
                    age = info.get("last_poll_seconds_ago")
                    if pct is not None:
                        bar_filled = int(pct) // 10
                        bar = "█" * bar_filled + "░" * (10 - bar_filled)
                        age_str = f" ({age}s ago)" if age is not None else ""
                        lines.append(f"  {provider_name}: {pct:.1f}% [{bar}]{age_str}")
                    else:
                        lines.append(f"  {provider_name}: (pending first poll)")
        except Exception:
            pass

        return "\n".join(lines)

    async def cmd_model(args: str, ctx: CommandContext) -> str:
        if ctx.session is None:
            return "No active session."
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if ctx.gateway._agent_manager
            else None
        )
        if agent is None:
            return "No agent found for this session."

        base_model = agent.config.model
        current_model = ctx.session.context.get("model_override") or base_model

        # Route FA ACP model subcommands to the live runner
        first_word = args.strip().split()[0].lower() if args.strip() else ""
        if first_word in _FA_MODEL_SUBCOMMANDS:
            runner = _get_runner(ctx)
            if runner is None:
                return "No active agent session — send a message first, then retry."
            return await runner.acp_execute("model", args.strip())

        if not args.strip():
            lines = [f"Current model: {current_model}"]
            if current_model != base_model:
                lines.append(f"Default: {base_model}")
            # Show runner model and API endpoint for verification
            runner = getattr(agent, "fast_agent_runner", None)
            if runner:
                lines.append(f"Runner model: {runner.model}")
            import os
            base_url = os.environ.get("GENERIC_BASE_URL", "")
            if base_url:
                lines.append(f"API endpoint: {base_url}")
            api_key_set = bool(os.environ.get("GENERIC_API_KEY") or (runner and getattr(runner, "api_key", None)))
            lines.append(f"API key: {'set ✅' if api_key_set else 'missing ❌'}")
            lines.append(f"\nFA subcommands: /model reasoning|fast|verbosity|web_search|web_fetch|doctor|aliases|catalog")
            return "\n".join(lines)

        new_model = args.strip()
        ctx.session.context["model_override"] = new_model
        # Clear the runner so it is recreated with the new model on the next message.
        agent._session_runners.pop(ctx.session.id, None)
        return f"✅ Model set to: {new_model}\n(Takes effect on next message)"

    async def cmd_job(args: str, ctx: CommandContext) -> str:
        # Reconstruct full /job … text and delegate to gateway's parser.
        full = f"/job {args}".strip()
        return await ctx.gateway._handle_job_command(full)

    async def cmd_start(args: str, ctx: CommandContext) -> str:
        return (
            "👋 Hello! I'm your pyclopse assistant.\n\n"
            + registry.help_text()
        )

    # ------------------------------------------------------------------ new
    async def cmd_new(args: str, ctx: CommandContext) -> str:
        """Start a brand-new session, discarding the current one."""
        if ctx.session is None:
            return "No active session."
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if ctx.gateway._agent_manager else None
        )
        old_id = ctx.session.id[:8]
        # Evict the runner for the OLD session so it releases its FA context
        if agent:
            await agent.evict_session_runner(ctx.session.id)
        # Create a new session with a new ID — this gives a fresh history_path
        # so the new runner starts with an empty conversation, not the old one.
        sm = ctx.gateway._session_manager
        if sm:
            channel = ctx.session.last_channel or ctx.session.channel
            user_id = ctx.session.last_user_id or ctx.session.user_id
            thread_ts = ctx.session.last_thread_ts
            new_session = await sm.create_session(
                agent_id=ctx.session.agent_id,
                channel=channel,
                user_id=user_id,
            )
            new_session.last_channel = channel
            new_session.last_user_id = user_id
            new_session.last_thread_ts = thread_ts
            new_session.save_metadata()
            sm.set_active_session(ctx.session.agent_id, new_session.id)
        return f"✅ New session started (was {old_id}…). Fresh context, no history."

    async def cmd_stop(args: str, ctx: CommandContext) -> str:
        """Cancel any in-progress agent call for this session."""
        gw = ctx.gateway
        active = getattr(gw, "_active_tasks", {})
        # Try session-specific key first, then channel:sender fallback
        keys_to_try = []
        if ctx.session:
            keys_to_try.append(ctx.session.id)
        keys_to_try.append(f"{ctx.channel}:{ctx.sender_id}")
        for key in keys_to_try:
            task = active.get(key)
            if task and not task.done():
                task.cancel()
                return "⛔ Cancelled current request."
        return "Nothing running for this session."

    async def cmd_compact(args: str, ctx: CommandContext) -> str:
        """Summarise and compress session history to save context space."""
        if ctx.session is None:
            return "No active session."
        msgs = ctx.session.get_messages()
        if len(msgs) < 4:
            return "Session is too short to compact."
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if ctx.gateway._agent_manager else None
        )
        if not agent or not agent.fast_agent_runner:
            return "No agent runner available."
        extra = f" {args.strip()}" if args.strip() else ""
        summary_prompt = (
            f"Summarise this conversation into a concise briefing that preserves all "
            f"key facts, decisions, and context needed to continue the work.{extra}\n\n"
            + "\n".join(
                f"{m.get('role','?').upper()}: {str(m.get('content',''))[:500]}"
                for m in msgs[-40:]
            )
        )
        try:
            summary = await agent.fast_agent_runner.run(summary_prompt)
        except Exception as e:
            return f"[ERROR] Compact failed: {e}"
        ctx.session.clear_messages()
        ctx.session.add_message(role="system", content=f"[Compacted context]\n{summary}")
        if agent:
            agent._session_runners.pop(ctx.session.id, None)
        return f"✅ Compacted {len(msgs)} messages → 1 summary.\n\n{summary[:300]}…"

    async def cmd_whoami(args: str, ctx: CommandContext) -> str:
        """Show your sender ID, session, and channel."""
        lines = [
            f"Sender ID: {ctx.sender_id}",
            f"Channel:   {ctx.channel}",
        ]
        if ctx.session:
            lines += [
                f"Session:   {ctx.session.id[:16]}…",
                f"Agent:     {ctx.session.agent_id}",
                f"Messages:  {len(ctx.session.get_messages())}",
            ]
        return "\n".join(lines)

    async def cmd_models(args: str, ctx: CommandContext) -> str:
        """List configured model providers, or manage fallback chain.

        Usage:
          /models                      — list providers and active model
          /models fallbacks            — show fallback chain for this agent
          /models fallbacks add <m>    — append model to fallback chain
          /models fallbacks remove <m> — remove model from fallback chain
          /models fallbacks clear      — clear entire fallback chain
        """
        first = args.strip().split()[0].lower() if args.strip() else ""

        if first == "fallbacks":
            if ctx.session is None:
                return "No active session."
            agent = (
                ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
                if ctx.gateway._agent_manager else None
            )
            if agent is None:
                return "No agent found for this session."
            fallbacks: list = list(getattr(agent.config, "fallbacks", None) or [])
            rest = args.strip()[len("fallbacks"):].strip()
            parts = rest.split(maxsplit=1)
            sub = parts[0].lower() if parts else "list"
            val = parts[1].strip() if len(parts) > 1 else ""

            if sub in ("list", "") or not rest:
                if not fallbacks:
                    return f"No fallbacks configured for agent {agent.config.name!r}."
                lines = [f"Fallback chain for {agent.config.name!r}:"]
                for i, m in enumerate(fallbacks, 1):
                    lines.append(f"  {i}. {m}")
                idx = ctx.session.context.get("_fallback_index", 0)
                if idx:
                    lines.append(f"Current position: {idx} ({fallbacks[idx] if idx < len(fallbacks) else 'exhausted'})")
                return "\n".join(lines)

            if sub == "add":
                if not val:
                    return "Usage: /models fallbacks add <model>"
                fallbacks.append(val)
                agent.config.fallbacks = fallbacks
                return f"✅ Added {val!r} to fallback chain (position {len(fallbacks)})."

            if sub in ("remove", "rm", "del"):
                if not val:
                    return "Usage: /models fallbacks remove <model>"
                if val not in fallbacks:
                    return f"{val!r} not in fallback chain."
                fallbacks.remove(val)
                agent.config.fallbacks = fallbacks
                ctx.session.context.pop("_fallback_index", None)
                return f"✅ Removed {val!r} from fallback chain."

            if sub == "clear":
                agent.config.fallbacks = []
                ctx.session.context.pop("_fallback_index", None)
                return "✅ Fallback chain cleared."

            return "Usage: /models fallbacks [list|add <model>|remove <model>|clear]"

        try:
            cfg = ctx.gateway.config
            providers = cfg.providers
            lines = ["Configured providers:"]
            for name in ("minimax", "openai", "anthropic", "google", "fastagent"):
                p = getattr(providers, name, None)
                if p and getattr(p, "enabled", False):
                    model = getattr(p, "default_model", None) or getattr(p, "model", "?")
                    key_set = bool(getattr(p, "api_key", None))
                    lines.append(f"  • {name}: {model} (key {'✅' if key_set else '❌'})")
            # Also show current session model
            if ctx.session:
                agent = (
                    ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
                    if ctx.gateway._agent_manager else None
                )
                if agent:
                    current = ctx.session.context.get("model_override") or agent.config.model
                    lines.append(f"\nActive model: {current}")
                    fallbacks = getattr(agent.config, "fallbacks", None) or []
                    if fallbacks:
                        lines.append(f"Fallbacks: {', '.join(fallbacks)}")
            return "\n".join(lines) if len(lines) > 1 else "No providers configured."
        except Exception as e:
            return f"[ERROR] {e}"

    async def cmd_think(args: str, ctx: CommandContext) -> str:
        """Set thinking budget for the current session.
        Levels: off, low (1k), medium (5k), high (10k), max (20k)"""
        if ctx.session is None:
            return "No active session."
        level = args.strip().lower() or "status"
        budget_map = {"off": 0, "low": 1024, "medium": 5120, "high": 10240, "max": 20480}
        if level == "status":
            current = ctx.session.context.get("thinking_budget", "off")
            return f"Thinking budget: {current}"
        if level not in budget_map:
            return f"Usage: /think [off|low|medium|high|max]\nCurrent: {ctx.session.context.get('thinking_budget', 'off')}"
        ctx.session.context["thinking_budget"] = level
        ctx.session.context["thinking_tokens"] = budget_map[level]
        # Recreate runner to pick up new setting
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if ctx.gateway._agent_manager else None
        )
        if agent:
            agent._session_runners.pop(ctx.session.id, None)
        if level == "off":
            return "🧠 Thinking disabled."
        return f"🧠 Thinking set to {level} ({budget_map[level]:,} tokens)."

    async def cmd_usage(args: str, ctx: CommandContext) -> str:
        """Show message counts, uptime, and provider quota usage."""
        import time
        gw_usage = getattr(ctx.gateway, "_usage", {})
        started = gw_usage.get("started_at", time.time())
        uptime_s = int(time.time() - started)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        lines = [
            f"Gateway uptime: {h}h {m}m {s}s",
            f"Total messages: {gw_usage.get('messages_total', 0)}",
        ]
        by_channel = gw_usage.get("messages_by_channel", {})
        if by_channel:
            lines.append("By channel: " + ", ".join(f"{k}={v}" for k, v in by_channel.items()))
        if ctx.session:
            lines += [
                f"\nThis session:",
                f"  Messages: {len(ctx.session.get_messages())}",
                f"  Session ID: {ctx.session.id[:16]}…",
            ]

        # Provider quota usage
        try:
            from pyclopse.core.usage import get_registry
            usage_status = get_registry().status()
            if usage_status:
                lines.append("\nProvider Quota:")
                for provider_name, info in usage_status.items():
                    pct = info.get("usage_pct")
                    age = info.get("last_poll_seconds_ago")
                    interval = info.get("check_interval", 300)
                    endpoint = info.get("endpoint", "")
                    if pct is not None:
                        bar_filled = int(pct) // 10
                        bar = "█" * bar_filled + "░" * (10 - bar_filled)
                        age_str = f"  (polled {age}s ago)" if age is not None else ""
                        lines.append(f"  {provider_name}: {pct:.1f}% [{bar}]{age_str}")
                        # Show throttle thresholds from config if accessible
                        try:
                            providers_cfg = ctx.gateway.config.providers
                            pcfg = (
                                getattr(providers_cfg, provider_name, None)
                                or (getattr(providers_cfg, "model_extra", None) or {}).get(provider_name)
                            )
                            if pcfg and getattr(pcfg, "usage", None):
                                t = pcfg.usage.throttle
                                lines.append(
                                    f"    Throttle: background≥{t.background}%  normal≥{t.normal}%"
                                )
                        except Exception:
                            pass
                    else:
                        lines.append(f"  {provider_name}: (pending first poll, interval={interval}s)")
        except Exception:
            pass

        return "\n".join(lines)

    async def cmd_context(args: str, ctx: CommandContext) -> str:
        """Show what context is currently held in this session."""
        if ctx.session is None:
            return "No active session."
        msgs = ctx.session.get_messages()
        if not msgs:
            return "Session context is empty."
        lines = [f"Session {ctx.session.id[:12]}… — {len(msgs)} messages:"]
        for i, m in enumerate(msgs[-10:], 1):
            role = m.get("role", "?").upper()[:9]
            content = str(m.get("content", ""))
            preview = content[:120].replace("\n", " ")
            lines.append(f"  {i}. [{role}] {preview}{'…' if len(content) > 120 else ''}")
        if len(msgs) > 10:
            lines.insert(1, f"  (showing last 10 of {len(msgs)})")
        extras = {k: v for k, v in ctx.session.context.items()
                  if k not in ("model_override",)}
        if extras:
            lines.append(f"\nSession context: {extras}")
        return "\n".join(lines)

    async def cmd_reload(args: str, ctx: CommandContext) -> str:
        """Reload config from disk and apply non-destructive changes."""
        try:
            from pyclopse.skills.registry import invalidate_skills_cache
            invalidate_skills_cache()
            changed = await ctx.gateway.reload_config()
            if changed:
                keys = ", ".join(changed.keys())
                return f"✅ Config reloaded. Changed: {keys}"
            return "✅ Config reloaded (no changes detected)."
        except Exception as e:
            return f"[ERROR] Reload failed: {e}"

    async def cmd_restart(args: str, ctx: CommandContext) -> str:
        """Reload config and reinitialize agents/channels."""
        try:
            await ctx.gateway.reload_config()
            # Reinitialize channels to pick up new bot tokens etc.
            await ctx.gateway._init_channels()
            # Re-register Telegram commands
            await ctx.gateway._register_telegram_commands()
            return "✅ Gateway restarted (config reloaded, channels reinitialized)."
        except Exception as e:
            return f"[ERROR] Restart failed: {e}"

    async def cmd_config(args: str, ctx: CommandContext) -> str:
        """Show or set config values. Usage: /config [get <key> | set <key> <value> | show]"""
        import yaml  # type: ignore
        from pathlib import Path
        cfg_path = Path("~/.pyclopse/config/pyclopse.yaml").expanduser()
        parts = args.strip().split(maxsplit=2)
        action = parts[0].lower() if parts else "show"

        if action in ("show", ""):
            if not cfg_path.exists():
                return "No config file found."
            return f"```\n{cfg_path.read_text()[:2000]}\n```"

        if action == "get":
            if len(parts) < 2:
                return "Usage: /config get <key.path>"
            key_path = parts[1].split(".")
            try:
                data = yaml.safe_load(cfg_path.read_text())
                val = data
                for k in key_path:
                    val = val[k]
                return f"{parts[1]} = {val}"
            except (KeyError, TypeError):
                return f"Key not found: {parts[1]}"
            except Exception as e:
                return f"[ERROR] {e}"

        if action == "set":
            if len(parts) < 3:
                return "Usage: /config set <key.path> <value>"
            return "⚠️ Live config edits are not yet supported via command. Edit ~/.pyclopse/config/pyclopse.yaml and use /reload."

        return "Usage: /config [show | get <key> | set <key> <value>]"

    async def cmd_export(args: str, ctx: CommandContext) -> str:
        """Export the current session history to a text file."""
        import json
        from pathlib import Path
        from datetime import datetime as _dt
        if ctx.session is None:
            return "No active session."
        msgs = ctx.session.get_messages()
        if not msgs:
            return "Session is empty, nothing to export."
        out_path = args.strip() or f"/tmp/session_{ctx.session.id[:8]}_{_dt.now().strftime('%Y%m%d_%H%M%S')}.txt"
        lines = [f"Session: {ctx.session.id}", f"Agent: {ctx.session.agent_id}",
                 f"Channel: {ctx.channel}", f"Messages: {len(msgs)}", "=" * 60, ""]
        for m in msgs:
            role = m.get("role", "?").upper()
            content = str(m.get("content", ""))
            lines += [f"[{role}]", content, ""]
        try:
            Path(out_path).write_text("\n".join(lines))
            return f"✅ Session exported to: {out_path}"
        except Exception as e:
            return f"[ERROR] Export failed: {e}"

    async def cmd_verbose(args: str, ctx: CommandContext) -> str:
        """Toggle verbose/debug output for this session."""
        if ctx.session is None:
            return "No active session."
        current = ctx.session.context.get("verbose", False)
        arg = args.strip().lower()
        if arg in ("on", "true", "1"):
            new_val = True
        elif arg in ("off", "false", "0"):
            new_val = False
        else:
            new_val = not current
        ctx.session.context["verbose"] = new_val
        return f"Verbose mode: {'ON 🔊' if new_val else 'OFF 🔇'}"

    async def cmd_approve(args: str, ctx: CommandContext) -> str:
        """Manage exec approval allowlist. Usage: /approve [list | add <pattern> | remove <pattern>]"""
        approval_sys = getattr(ctx.gateway, "_approval_system", None)
        if not approval_sys:
            return "Approval system not configured."
        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        if action == "list":
            patterns = [p.pattern for p in getattr(approval_sys, "always_approve", [])]
            if not patterns:
                return "Allowlist is empty. All commands require approval."
            return "Always-approved patterns:\n" + "\n".join(f"  • {p}" for p in patterns)
        if action == "add":
            if len(parts) < 2:
                return "Usage: /approve add <pattern>"
            import re
            pattern = parts[1].strip()
            approval_sys.always_approve.append(re.compile(pattern, re.IGNORECASE))
            return f"✅ Added to allowlist: {pattern}"
        if action in ("remove", "del"):
            if len(parts) < 2:
                return "Usage: /approve remove <pattern>"
            before = len(approval_sys.always_approve)
            approval_sys.always_approve = [
                p for p in approval_sys.always_approve
                if p.pattern != parts[1].strip()
            ]
            removed = before - len(approval_sys.always_approve)
            return f"✅ Removed {removed} pattern(s)." if removed else "Pattern not found."
        return "Usage: /approve [list | add <pattern> | remove <pattern>]"

    async def cmd_skills(args: str, ctx: CommandContext) -> str:
        """List available skills with their descriptions."""
        try:
            from pyclopse.skills.registry import discover_skills
            agent_id = ctx.session.agent_id if ctx.session else None
            skills = discover_skills(agent_name=agent_id)
            if not skills:
                return "No skills installed. Add skill directories to ~/.pyclopse/skills/."
            lines = [f"Available skills ({len(skills)}):"]
            for s in sorted(skills, key=lambda x: x.name):
                lines.append(f"  • {s.name} — {s.description}")
            lines.append("\nUse /skill <name> [args] to invoke a skill.")
            return "\n".join(lines)
        except Exception as e:
            return f"[ERROR] {e}"

    async def cmd_skill(args: str, ctx: CommandContext) -> str:
        """Invoke a skill by name, injecting its content into the agent.
        Usage: /skill <name> [optional args or question]
        """
        if not args.strip():
            return "Usage: /skill <name> [args]\nSee /skills for available skills."
        parts = args.strip().split(maxsplit=1)
        skill_name = parts[0]
        skill_args = parts[1] if len(parts) > 1 else ""

        try:
            from pyclopse.skills.registry import find_skill
            agent_id = ctx.session.agent_id if ctx.session else None
            skill = find_skill(skill_name, agent_name=agent_id)
            if skill is None:
                return (
                    f"Skill {skill_name!r} not found. Use /skills to list available skills."
                )

            # Build a message that injects the skill content and optional user args
            content = skill.read_content()
            if skill_args:
                message = (
                    f"[Skill: {skill.name}]\n\n{content}\n\n"
                    f"--- User request ---\n{skill_args}"
                )
            else:
                message = f"[Skill: {skill.name}]\n\n{content}"

            # Forward to the agent as a regular message
            agent = (
                ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
                if ctx.gateway._agent_manager and ctx.session
                else None
            )
            if agent is None:
                return f"[ERROR] No agent found for this session."

            from pyclopse.core.router import IncomingMessage
            msg = IncomingMessage(
                content=message,
                sender=ctx.sender_id,
                sender_id=ctx.sender_id,
                channel=ctx.channel,
            )
            result = await agent.handle_message(msg, ctx.session)
            return result.content if result else "[No response]"
        except Exception as e:
            logger.error(f"/skill {skill_name} failed: {e}", exc_info=True)
            return f"[ERROR] {e}"

    async def cmd_reboot(args: str, ctx: CommandContext) -> str:
        """Hard-restart the pyclopse process (picks up code + config changes)."""
        import asyncio
        import os
        import sys

        async def _do_reboot():
            await asyncio.sleep(0.5)
            # Flush state (scheduler merge-save, session saves, etc.) before
            # replacing the process so nothing is lost across the restart.
            try:
                await ctx.gateway.stop()
            except Exception:
                pass
            # When run via `python -m pyclopse`, sys.argv[0] is __main__.py.
            # Re-executing that path directly breaks relative imports, so we
            # rebuild the argv using -m instead.
            if sys.argv[0].endswith("__main__.py"):
                argv = [sys.executable, "-m", "pyclopse"] + sys.argv[1:]
            else:
                argv = [sys.executable] + sys.argv
            os.execv(sys.executable, argv)

        asyncio.create_task(_do_reboot())
        return "🔄 Rebooting pyclopse… (back in a few seconds)"

    async def cmd_subagents(args: str, ctx: CommandContext) -> str:
        """Manage background subagents. Usage: /subagents [list|status|kill|interrupt|send]"""
        import httpx

        gw = ctx.gateway
        scheduler = getattr(gw, "_job_scheduler", None)
        if not scheduler:
            return "Job scheduler not available."

        parts = args.strip().split(maxsplit=2) if args.strip() else []
        sub = parts[0].lower() if parts else "list"

        # Determine caller's agent and session
        agent_id = ctx.session.agent_id if ctx.session else None
        session_id = ctx.session.id if ctx.session else None

        if sub in ("list", ""):
            entries = scheduler.list_subagents(spawned_by_agent=agent_id)
            if not entries:
                return "No active subagents."
            lines = [f"Active subagents ({len(entries)}):"]
            for job, sess in entries:
                lines.append(
                    f"  [{job.status.value}] {job.name}  "
                    f"id={job.id[:8]}…  session={sess[:8] if sess else '—'}…"
                )
                lines.append(f"    task: {str(getattr(job.run, 'message', ''))[:100]}")
            return "\n".join(lines)

        if sub == "status":
            if len(parts) < 2:
                return "Usage: /subagents status <job_id>"
            job_id = parts[1]
            job = scheduler.jobs.get(job_id) or next(
                (j for j in scheduler.jobs.values()
                 if j.name == job_id and j.spawned_by_session is not None), None
            )
            if not job or not job.spawned_by_session is not None:
                return f"Subagent '{job_id}' not found."
            sess = scheduler._subagent_sessions.get(job.id, "—")
            queued = len(scheduler._subagent_message_queue.get(job.id, []))
            lines = [
                f"Subagent: {job.name}",
                f"Status:   {job.status.value}",
                f"Agent:    {getattr(job.run, 'agent', '?')}",
                f"Session:  {sess[:16]}…" if sess != "—" else "Session:  —",
                f"Queued messages: {queued}",
                f"Task:     {str(getattr(job.run, 'message', ''))[:200]}",
            ]
            return "\n".join(lines)

        if sub == "kill":
            if len(parts) < 2:
                return "Usage: /subagents kill <job_id>"
            job_id = parts[1]
            killed = await scheduler.kill_subagent(job_id)
            return f"✅ Subagent {job_id[:8]}… killed." if killed else f"Subagent '{job_id}' not found or not running."

        if sub == "interrupt":
            if len(parts) < 3:
                return "Usage: /subagents interrupt <job_id> <new task>"
            job_id = parts[1]
            new_task = parts[2]
            job = scheduler.jobs.get(job_id)
            if not job or not job.spawned_by_session is not None:
                return f"Subagent '{job_id}' not found."
            old_run = job.run
            spawned_by = job.spawned_by_session or session_id or ""
            agent = getattr(old_run, "agent", agent_id or "")
            await scheduler.kill_subagent(job_id)
            new_id = await scheduler.spawn_subagent(
                task=new_task,
                agent=agent,
                spawned_by_session=spawned_by,
                model=getattr(old_run, "model", None),
                timeout_seconds=job.timeout_seconds,
                prompt_preset=getattr(old_run, "prompt_preset", "minimal"),
                instruction=getattr(old_run, "instruction", None),
            )
            return f"✅ Subagent interrupted. New subagent: {new_id[:8]}…"

        if sub == "send":
            if len(parts) < 3:
                return "Usage: /subagents send <job_id> <message>"
            job_id = parts[1]
            message = parts[2]
            queued = scheduler.queue_message(job_id, message)
            return (
                f"✅ Message queued for subagent {job_id[:8]}…"
                if queued else
                f"Subagent '{job_id}' not found — may have already completed."
            )

        return (
            "Usage: /subagents <subcommand>\n"
            "  list                        — list active subagents\n"
            "  status <id>                 — show subagent details\n"
            "  kill <id>                   — cancel a running subagent\n"
            "  interrupt <id> <new task>   — kill and respawn with new task\n"
            "  send <id> <message>         — queue a follow-up message"
        )

    async def cmd_queue(args: str, ctx: CommandContext) -> str:
        """Show or set the message queue mode for this session.

        Usage:
          /queue                       — show current queue config
          /queue mode=<mode>           — set mode (followup|collect|interrupt|steer|steer-backlog)
          /queue debounce=<ms>         — set debounce window in milliseconds
          /queue cap=<n>               — set max queue depth
          /queue drop=<old|new|summarize> — set overflow drop policy
          /queue reset                 — restore agent default config
        """
        from pyclopse.config.schema import QueueConfig, QueueMode, DropPolicy

        if ctx.session is None:
            return "No active session."

        session_key = f"{ctx.channel}:{ctx.sender_id}"
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if getattr(ctx.gateway, "_agent_manager", None) else None
        )
        base_cfg: QueueConfig = (
            agent.config.queue
            if agent and hasattr(agent.config, "queue")
            else QueueConfig()
        )
        qm = getattr(ctx.gateway, "_queue_manager", None)

        stripped = args.strip().lower()

        if not stripped or stripped == "status":
            overrides = qm.get_config_override(session_key) if qm else {}
            effective_mode = overrides.get("mode") or base_cfg.mode.value
            effective_debounce = overrides.get("debounce_ms", base_cfg.debounce_ms)
            effective_cap = overrides.get("cap", base_cfg.cap)
            effective_drop = overrides.get("drop") or base_cfg.drop.value
            lines = [
                "Queue config (this session):",
                f"  mode:     {effective_mode}",
                f"  debounce: {effective_debounce} ms",
                f"  cap:      {effective_cap}",
                f"  drop:     {effective_drop}",
            ]
            if overrides:
                lines.append("  (session override active — /queue reset to restore agent default)")
            return "\n".join(lines)

        if stripped == "reset":
            if qm:
                qm.update_config(
                    session_key,
                    mode=base_cfg.mode.value,
                    debounce_ms=base_cfg.debounce_ms,
                    cap=base_cfg.cap,
                    drop=base_cfg.drop.value,
                )
                # Clear the stored override
                qm._config_overrides.pop(session_key, None)
            return "Queue config reset to agent default."

        new_updates: dict = {}
        errors = []
        for part in args.strip().split():
            if "=" not in part:
                errors.append(f"Invalid argument: {part!r} (expected key=value)")
                continue
            k, v = part.split("=", 1)
            k = k.lower()
            if k == "mode":
                try:
                    QueueMode(v)
                    new_updates["mode"] = v
                except ValueError:
                    errors.append(f"Invalid mode {v!r}. Valid: {[m.value for m in QueueMode]}")
            elif k == "debounce":
                try:
                    new_updates["debounce_ms"] = int(v)
                except ValueError:
                    errors.append(f"Invalid debounce value: {v!r} (expected integer ms)")
            elif k == "cap":
                try:
                    new_updates["cap"] = int(v)
                except ValueError:
                    errors.append(f"Invalid cap value: {v!r} (expected integer)")
            elif k == "drop":
                try:
                    DropPolicy(v)
                    new_updates["drop"] = v
                except ValueError:
                    errors.append(f"Invalid drop policy {v!r}. Valid: {[d.value for d in DropPolicy]}")
            else:
                errors.append(f"Unknown key: {k!r}")

        if errors:
            return "\n".join(errors)

        if qm and new_updates:
            qm.update_config(session_key, **new_updates)

        parts_set = [f"{k}={v}" for k, v in new_updates.items()]
        return f"Queue updated: {', '.join(parts_set)}"

    async def cmd_tts(args: str, ctx: CommandContext) -> str:
        """Toggle TTS output for this session. Usage: /tts [on|off|status]"""
        if ctx.session is None:
            return "No active session."
        arg = args.strip().lower() or "status"
        if arg == "status":
            val = ctx.session.context.get("tts_enabled", False)
            return f"TTS: {'ON 🔊' if val else 'OFF 🔇'}"
        if arg == "on":
            ctx.session.context["tts_enabled"] = True
            return "🔊 TTS enabled — agent responses will be converted to speech."
        if arg == "off":
            ctx.session.context["tts_enabled"] = False
            return "🔇 TTS disabled."
        return "Usage: /tts [on|off|status]"

    async def cmd_history(args: str, ctx: CommandContext) -> str:
        """Show, save, or load session history via FastAgent ACP.

        Usage: /history [show|save [name]|load [name]]
        """
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("history", args.strip())

    async def cmd_clear(args: str, ctx: CommandContext) -> str:
        """Clear conversation history (or just the last turn) via FastAgent ACP.

        Usage: /clear [last]
          /clear       — clear entire conversation history
          /clear last  — remove only the most recent exchange (undo last turn)
        """
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("clear", args.strip())

    # ── FA ACP pass-through handlers ──────────────────────────────────────────

    async def cmd_mcp(args: str, ctx: CommandContext) -> str:
        """Manage MCP servers at runtime via FastAgent ACP."""
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("mcp", args.strip())

    async def cmd_cards(args: str, ctx: CommandContext) -> str:
        """List or inspect agent cards via FastAgent ACP."""
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("cards", args.strip())

    async def cmd_card(args: str, ctx: CommandContext) -> str:
        """Inspect a specific agent card via FastAgent ACP."""
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("card", args.strip())

    async def cmd_agent(args: str, ctx: CommandContext) -> str:
        """Agent introspection and attachment via FastAgent ACP."""
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("agent", args.strip())

    # ── New commands ──────────────────────────────────────────────────────────

    async def cmd_bash(args: str, ctx: CommandContext) -> str:
        """Run a shell command and send the output to the agent as context.

        Usage: /bash <command>
        The command runs in a subprocess; stdout+stderr are captured and forwarded
        to the agent as a user message so it can act on the results.
        """
        if not args.strip():
            return "Usage: /bash <command>"
        import asyncio as _asyncio
        try:
            proc = await _asyncio.create_subprocess_shell(
                args,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.STDOUT,
            )
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip() or "(no output)"
        except _asyncio.TimeoutError:
            return "[ERROR] Command timed out after 30s"
        except Exception as e:
            return f"[ERROR] {e}"

        # Forward to the agent as context if a session exists
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if ctx.gateway._agent_manager and ctx.session else None
        )
        if agent and ctx.session:
            import uuid as _uuid
            from pyclopse.core.router import IncomingMessage
            msg = IncomingMessage(
                id=str(_uuid.uuid4()),
                content=f"[Shell: $ {args}]\n{output[:8000]}",
                sender=ctx.sender_id,
                sender_id=ctx.sender_id,
                channel=ctx.channel,
            )
            result = await agent.handle_message(msg, ctx.session)
            return result.content if result else f"```\n{output[:3000]}\n```"
        return f"```\n{output[:3000]}\n```"

    async def cmd_allowlist(args: str, ctx: CommandContext) -> str:
        """Manage the channel allowlist at runtime.

        Usage: /allowlist [list | add <id> | remove <id>]
        Changes take effect immediately and persist until gateway restart.
        To make permanent, edit config and /reload.
        """
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""

        cfg = ctx.gateway.config
        channel = ctx.channel

        try:
            if channel == "telegram":
                allowlist = cfg.channels.telegram.allowed_users  # List[int]
                def _parse(s: str): return int(s)
            elif channel == "slack":
                allowlist = cfg.channels.slack.allowed_users  # List[str]
                def _parse(s: str): return s.strip()
            else:
                return f"Allowlist management not supported for channel: {channel}"
        except AttributeError:
            return "Channel config not available."

        if sub == "list":
            if not allowlist:
                return "Allowlist is empty — all users are allowed."
            return "Allowlist:\n" + "\n".join(f"  • {uid}" for uid in allowlist)

        if sub == "add":
            if not rest:
                return "Usage: /allowlist add <id>"
            try:
                uid = _parse(rest)
                if uid not in allowlist:
                    allowlist.append(uid)
                return f"✅ Added {uid} to allowlist."
            except ValueError:
                return f"Invalid ID: {rest!r}"

        if sub in ("remove", "del"):
            if not rest:
                return "Usage: /allowlist remove <id>"
            try:
                uid = _parse(rest)
                if uid in allowlist:
                    allowlist.remove(uid)
                    return f"✅ Removed {uid} from allowlist."
                return f"{uid} not in allowlist."
            except ValueError:
                return f"Invalid ID: {rest!r}"

        return "Usage: /allowlist [list | add <id> | remove <id>]"

    async def cmd_reasoning(args: str, ctx: CommandContext) -> str:
        """Toggle reasoning output visibility for this session.

        Usage: /reasoning [on|stream|off|status]
          on / stream  — show <thinking> blocks in responses
          off          — strip thinking blocks (default)
          status       — show current setting
        Note: /think controls the thinking *budget*; /reasoning controls
        whether thinking output is *shown* to you.
        """
        if ctx.session is None:
            return "No active session."
        sub = args.strip().lower() or "status"

        if sub == "status":
            current = ctx.session.context.get("show_thinking", False)
            return f"Reasoning output: {'ON' if current else 'OFF'}"

        if sub in ("on", "stream"):
            new_val = True
        elif sub == "off":
            new_val = False
        else:
            return "Usage: /reasoning [on|stream|off|status]"

        ctx.session.context["show_thinking"] = new_val

        # Apply to the live runner immediately if it exists
        runner = _get_runner(ctx)
        if runner is not None:
            runner.show_thinking = new_val

        return f"Reasoning output: {'ON 🧠' if new_val else 'OFF'}"

    async def cmd_session(args: str, ctx: CommandContext) -> str:
        """Show or configure the current session.

        Usage:
          /session            — show session details
          /session timeout <minutes>  — set idle timeout for this session
          /session window <n>         — set rolling message window size
        """
        if ctx.session is None:
            return "No active session."

        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        val = parts[1].strip() if len(parts) > 1 else ""

        if not sub:
            from pyclopse.utils.time import now as _now
            s = ctx.session
            agent = (
                ctx.gateway._agent_manager.get_agent(s.agent_id)
                if ctx.gateway._agent_manager else None
            )
            current_model = s.context.get("model_override") or (
                agent.config.model if agent else "?"
            )
            idle_s = int((_now() - s.updated_at).total_seconds())
            lines = [
                f"Session: {s.id}",
                f"Agent:   {s.agent_id}",
                f"Channel: {s.last_channel or s.channel}",
                f"User:    {s.last_user_id or s.user_id}",
                f"Model:   {current_model}",
                f"Messages: {s.message_count}",
                f"Idle:    {idle_s}s",
                f"Created: {s.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
            ]
            overrides = {k: v for k, v in s.context.items()
                         if k in ("model_override", "show_thinking", "thinking_budget",
                                  "idle_timeout_minutes", "window_size")}
            if overrides:
                lines.append(f"Overrides: {overrides}")
            return "\n".join(lines)

        if sub == "timeout":
            try:
                minutes = int(val)
                ctx.session.context["idle_timeout_minutes"] = minutes
                return f"✅ Idle timeout set to {minutes} minutes for this session."
            except ValueError:
                return "Usage: /session timeout <minutes>"

        if sub == "window":
            try:
                n = int(val)
                ctx.session.context["window_size"] = n
                return f"✅ Message window set to {n} for this session."
            except ValueError:
                return "Usage: /session window <n>"

        return "Usage: /session [timeout <minutes> | window <n>]"

    async def cmd_acp(args: str, ctx: CommandContext) -> str:
        """ACP session management via FastAgent ACP.

        Usage: /acp [spawn|cancel|steer|close|sessions|status|set-mode|
                     set|cwd|permissions|timeout|model|reset-options|doctor|install|help]
        """
        runner = _get_runner(ctx)
        if runner is None:
            return "No active agent session — send a message first, then retry."
        return await runner.acp_execute("acp", args.strip())

    async def cmd_exec(args: str, ctx: CommandContext) -> str:
        """Set per-session exec defaults.

        Usage:
          /exec                          — show current exec settings
          /exec host <sandbox|gateway|node>   — set execution host
          /exec level <strict|normal|permissive> — set security level
          /exec ask <always|never|inherit>    — set approval ask policy
          /exec reset                    — restore defaults
        """
        if ctx.session is None:
            return "No active session."
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        val = parts[1].strip() if len(parts) > 1 else ""

        if not sub or sub == "show":
            host = ctx.session.context.get("exec_host", "gateway")
            level = ctx.session.context.get("exec_level", "normal")
            ask = ctx.session.context.get("exec_ask", "inherit")
            return (
                "Exec settings (this session):\n"
                f"  host:  {host}\n"
                f"  level: {level}\n"
                f"  ask:   {ask}"
            )

        if sub == "reset":
            for k in ("exec_host", "exec_level", "exec_ask"):
                ctx.session.context.pop(k, None)
            return "✅ Exec settings reset to defaults."

        if sub == "host":
            valid = {"sandbox", "gateway", "node"}
            if val not in valid:
                return f"Usage: /exec host <{'|'.join(sorted(valid))}>"
            ctx.session.context["exec_host"] = val
            return f"✅ Exec host: {val}"

        if sub == "level":
            valid = {"strict", "normal", "permissive"}
            if val not in valid:
                return f"Usage: /exec level <{'|'.join(sorted(valid))}>"
            ctx.session.context["exec_level"] = val
            return f"✅ Exec level: {val}"

        if sub == "ask":
            valid = {"always", "never", "inherit"}
            if val not in valid:
                return f"Usage: /exec ask <{'|'.join(sorted(valid))}>"
            ctx.session.context["exec_ask"] = val
            return f"✅ Exec ask: {val}"

        return "Usage: /exec [host|level|ask|reset]"

    async def cmd_send(args: str, ctx: CommandContext) -> str:
        """Set outbound send policy for this session.

        Usage:
          /send              — show current policy
          /send on           — send replies to the channel (default)
          /send off          — process messages silently (no outbound reply)
          /send inherit      — use the agent's default policy
        """
        if ctx.session is None:
            return "No active session."
        sub = args.strip().lower() or "status"

        if sub == "status":
            policy = ctx.session.context.get("send_policy", "on")
            return f"Send policy: {policy}"

        if sub not in ("on", "off", "inherit"):
            return "Usage: /send [on|off|inherit]"

        ctx.session.context["send_policy"] = sub
        descs = {
            "on": "Replies will be sent to the channel.",
            "off": "Messages processed silently — no replies sent.",
            "inherit": "Using agent default policy.",
        }
        return f"✅ Send policy: {sub} — {descs[sub]}"

    async def cmd_focus(args: str, ctx: CommandContext) -> str:
        """Bind the current thread/topic to an agent session.

        Usage:
          /focus              — bind to current session's agent
          /focus <agent_id>   — bind to a specific agent

        Once bound, all messages in this thread always route to the bound agent,
        regardless of which user sends them.  Use /unfocus to remove.
        """
        if ctx.session is None:
            return "No active session."
        if ctx.thread_id is None:
            return (
                "No thread ID detected. /focus works in Telegram topics and "
                "Slack threads, not in direct messages."
            )
        target_agent = args.strip() or ctx.session.agent_id
        # Validate agent exists
        am = getattr(ctx.gateway, "_agent_manager", None)
        if am and am.get_agent(target_agent) is None:
            agent_ids = list(getattr(am, "agents", {}).keys())
            return (
                f"Agent {target_agent!r} not found.\n"
                f"Available: {', '.join(agent_ids)}"
            )
        binding_key = f"{ctx.channel}:{ctx.thread_id}"
        if not hasattr(ctx.gateway, "_thread_bindings"):
            ctx.gateway._thread_bindings = {}
        ctx.gateway._thread_bindings[binding_key] = target_agent
        return f"✅ Thread bound to agent {target_agent!r}. Use /unfocus to remove."

    async def cmd_unfocus(args: str, ctx: CommandContext) -> str:
        """Remove the thread/topic binding for the current thread.

        Usage: /unfocus
        """
        if ctx.thread_id is None:
            return "No thread ID detected — not in a thread/topic."
        binding_key = f"{ctx.channel}:{ctx.thread_id}"
        bindings = getattr(ctx.gateway, "_thread_bindings", {})
        if binding_key in bindings:
            agent_id = bindings.pop(binding_key)
            return f"✅ Thread unbound (was: {agent_id!r})."
        return "This thread has no binding."

    async def cmd_agents(args: str, ctx: CommandContext) -> str:
        """List thread/topic bindings for the current channel.

        Usage: /agents
        """
        bindings = getattr(ctx.gateway, "_thread_bindings", {})
        channel_bindings = {
            k: v for k, v in bindings.items()
            if k.startswith(f"{ctx.channel}:")
        }
        if not channel_bindings:
            return f"No thread bindings for channel {ctx.channel!r}."
        lines = [f"Thread bindings ({ctx.channel}):"]
        for key, agent_id in sorted(channel_bindings.items()):
            thread = key.split(":", 1)[1]
            marker = " ← current" if thread == ctx.thread_id else ""
            lines.append(f"  {thread}: → {agent_id}{marker}")
        return "\n".join(lines)

    async def cmd_commands(args: str, ctx: CommandContext) -> str:
        """Show all commands in a compact single-line format."""
        if not registry._commands:
            return "No commands registered."
        # Two-column compact table
        cmds = sorted(registry._commands.values(), key=lambda c: c.name)
        col_w = max(len(c.name) for c in cmds) + 1
        lines = []
        for c in cmds:
            lines.append(f"  /{c.name:<{col_w}} {c.description}")
        return "Commands:\n" + "\n".join(lines)

    async def cmd_debug(args: str, ctx: CommandContext) -> str:
        """Runtime debug overrides for this session.

        Usage:
          /debug              — show current debug flags
          /debug set <k> <v>  — set a debug flag
          /debug unset <k>    — remove a debug flag
          /debug reset        — clear all debug flags
        """
        if ctx.session is None:
            return "No active session."
        dbg: dict = ctx.session.context.setdefault("_debug", {})
        parts = args.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else "show"

        if sub in ("show", ""):
            if not dbg:
                return "No debug flags set."
            lines = ["Debug flags:"]
            for k, v in sorted(dbg.items()):
                lines.append(f"  {k} = {v!r}")
            return "\n".join(lines)

        if sub == "set":
            if len(parts) < 3:
                return "Usage: /debug set <key> <value>"
            k, v = parts[1], parts[2]
            # Try to coerce to int/bool/float before storing as string
            if v.lower() in ("true", "yes", "1"):
                dbg[k] = True
            elif v.lower() in ("false", "no", "0"):
                dbg[k] = False
            else:
                try:
                    dbg[k] = int(v)
                except ValueError:
                    try:
                        dbg[k] = float(v)
                    except ValueError:
                        dbg[k] = v
            return f"✅ debug.{k} = {dbg[k]!r}"

        if sub == "unset":
            if len(parts) < 2:
                return "Usage: /debug unset <key>"
            k = parts[1]
            if k in dbg:
                del dbg[k]
                return f"✅ Unset debug.{k}"
            return f"Flag {k!r} not set."

        if sub == "reset":
            ctx.session.context.pop("_debug", None)
            return "✅ Debug flags cleared."

        return "Usage: /debug [show | set <k> <v> | unset <k> | reset]"

    async def cmd_activation(args: str, ctx: CommandContext) -> str:
        """Set group activation mode for this session.

        Usage:
          /activation             — show current mode
          /activation always      — respond to every message (default)
          /activation mention     — respond only when the bot is mentioned by name
        """
        if ctx.session is None:
            return "No active session."
        sub = args.strip().lower() or "status"

        if sub == "status":
            mode = ctx.session.context.get("activation_mode", "always")
            return f"Activation mode: {mode}"

        if sub in ("always", "mention"):
            ctx.session.context["activation_mode"] = sub
            desc = (
                "Responds to every message."
                if sub == "always"
                else "Responds only when mentioned by name."
            )
            return f"✅ Activation mode: {sub} — {desc}"

        return "Usage: /activation [always|mention]"

    async def cmd_elevated(args: str, ctx: CommandContext) -> str:
        """Toggle elevated exec approval mode for this session.

        Usage:
          /elevated              — show current mode
          /elevated on           — pre-approve all exec commands this session
          /elevated off          — restore normal approval (default)
          /elevated ask          — prompt for approval on every command
          /elevated full         — unrestricted, no approval checks

        Note: Changes persist only for the current session.
        """
        if ctx.session is None:
            return "No active session."
        sub = args.strip().lower() or "status"

        valid_modes = {"on", "off", "ask", "full"}

        if sub == "status":
            mode = ctx.session.context.get("elevated_mode", "off")
            return f"Elevated mode: {mode}"

        if sub not in valid_modes:
            return f"Usage: /elevated [on|off|ask|full]\nCurrent: {ctx.session.context.get('elevated_mode', 'off')}"

        ctx.session.context["elevated_mode"] = sub

        # Apply to live approval system if available
        approval_sys = getattr(ctx.gateway, "_approval_system", None)
        if approval_sys:
            import re as _re
            if sub == "on":
                # Pre-approve everything by adding a catch-all pattern
                catch_all = _re.compile(r".*")
                already = any(p.pattern == ".*" for p in getattr(approval_sys, "always_approve", []))
                if not already:
                    approval_sys.always_approve.append(catch_all)
                    ctx.session.context["_elevated_catch_all"] = True
            elif sub in ("off", "ask"):
                # Remove any session-added catch-all
                if ctx.session.context.pop("_elevated_catch_all", False):
                    approval_sys.always_approve = [
                        p for p in getattr(approval_sys, "always_approve", [])
                        if p.pattern != ".*"
                    ]

        descriptions = {
            "on":   "All exec commands pre-approved for this session.",
            "off":  "Normal approval mode restored.",
            "ask":  "Approval required for every exec command.",
            "full": "Unrestricted — all approval checks bypassed.",
        }
        return f"✅ Elevated mode: {sub} — {descriptions[sub]}"

    def _get_agent_vault(ctx: CommandContext):
        """Return the vault store for the agent associated with ctx.session, or None."""
        if ctx.session is None:
            return None
        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if getattr(ctx.gateway, "_agent_manager", None) else None
        )
        if agent is None:
            return None
        return getattr(agent, "_vault_store", None)

    async def cmd_memories(args: str, ctx: CommandContext) -> str:
        """List or search vault memory facts for the current agent.

        Usage:
          /memories              — list all crystallized facts
          /memories <query>      — search facts relevant to query
          /memories --all        — include provisional facts too
          /memories --type <t>   — filter by type (preference, decision, ...)
        """
        store = _get_agent_vault(ctx)
        if store is None:
            return "Vault memory is not available for this agent (not configured or not yet initialised)."

        try:
            from pyclopse.memory.vault.models import VaultFactState
            from pyclopse.memory.vault.retrieval import FallbackSearchBackend  # noqa: F401

            parts = args.strip().split()
            include_all = "--all" in parts
            type_filter = None
            query = []
            i = 0
            while i < len(parts):
                if parts[i] == "--all":
                    i += 1
                elif parts[i] == "--type" and i + 1 < len(parts):
                    type_filter = parts[i + 1]
                    i += 2
                else:
                    query.append(parts[i])
                    i += 1
            query_str = " ".join(query)

            states = None if include_all else {VaultFactState.CRYSTALLIZED}
            types = {type_filter} if type_filter else None
            facts = store.list_facts(states=states, types=types)

            if not facts:
                qualifier = "" if include_all else " crystallized"
                return f"No{qualifier} memories found."

            # If a query was given, do a simple keyword filter on claim text
            if query_str:
                q_lower = query_str.lower()
                facts = [f for f in facts if q_lower in f.claim.lower() or (f.body and q_lower in f.body.lower())]
                if not facts:
                    return f"No memories matching: {query_str!r}"

            lines = [f"Memories ({len(facts)}):"]
            for f in facts:
                state_tag = f"/{f.state.value}" if include_all and f.state != VaultFactState.CRYSTALLIZED else ""
                line = f"  [{f.id[:8]}] ({f.type}{state_tag}) {f.claim}"
                if f.contrastive:
                    line += f" — {f.contrastive}"
                lines.append(line)
            lines.append("\nUse /forget <id> to archive a fact.")
            return "\n".join(lines)

        except Exception as e:
            return f"[ERROR] {e}"

    async def cmd_forget(args: str, ctx: CommandContext) -> str:
        """Archive (soft-delete) a vault memory fact by ID.

        Usage: /forget <fact-id>

        The fact ID can be the full ULID or just the first 8 characters
        (as shown in /memories output).  Archived facts are no longer
        included in context injection but are kept on disk.

        Example:
          /forget 01J8ZX3A
        """
        if not args.strip():
            return "Usage: /forget <fact-id>\nSee /memories for IDs."

        store = _get_agent_vault(ctx)
        if store is None:
            return "Vault memory is not configured for this agent."

        fact_id = args.strip()
        try:
            from pyclopse.memory.vault.models import VaultFactState

            # Support short IDs (first 8 chars) — scan list for match
            fact = store.read_fact(fact_id)
            if fact is None:
                # Try prefix match
                all_facts = store.list_facts(states=None, include_archive=False)
                matches = [f for f in all_facts if f.id.startswith(fact_id)]
                if len(matches) == 1:
                    fact = matches[0]
                elif len(matches) > 1:
                    return f"Ambiguous ID {fact_id!r} matches {len(matches)} facts — use a longer prefix."
                else:
                    return f"Fact {fact_id!r} not found."

            if fact.state == VaultFactState.ARCHIVED:
                return f"Fact [{fact.id[:8]}] is already archived."

            store.archive_fact(fact.id, reason="user:forget")
            return f"Archived [{fact.id[:8]}]: {fact.claim}"

        except Exception as e:
            return f"[ERROR] {e}"

    async def cmd_ingest(args: str, ctx: CommandContext) -> str:
        """Bulk-ingest session history and memory files into vault memory.

        Usage:
          /ingest              — ingest both sessions and memory files
          /ingest sessions     — session history only
          /ingest memory       — memory files only

        Safe to re-run — already-processed content is skipped.
        Rate-limit errors are retried automatically with back-off.
        """
        if ctx.session is None:
            return "No active session."

        agent = (
            ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if getattr(ctx.gateway, "_agent_manager", None) else None
        )
        if agent is None:
            return "No agent found for this session."

        if agent._vault_ingestion is None:
            return "Vault memory is not configured for this agent."

        sub = args.strip().lower()
        include_sessions = sub in ("", "sessions", "both")
        include_memory = sub in ("", "memory", "both")
        if not include_sessions and not include_memory:
            return "Usage: /ingest [sessions|memory]"

        from pathlib import Path
        from pyclopse.core.prompt_builder import get_agent_dir
        from pyclopse.memory.vault.bulk import BulkIngestor

        agent_dir = get_agent_dir(agent.id, agent.config_dir)
        lines: list[str] = [
            f"Starting bulk ingest for {agent.name}… "
            f"({'sessions + memory' if include_sessions and include_memory else sub})"
        ]

        async def _progress(msg: str) -> None:
            lines.append(msg)

        ingestor = BulkIngestor(
            agent_dir=agent_dir,
            ingestion_handler=agent._vault_ingestion,
            progress_callback=_progress,
        )
        try:
            await ingestor.run(
                include_sessions=include_sessions,
                include_memory=include_memory,
            )
        except Exception as e:
            lines.append(f"[ERROR] {e}")

        return "\n".join(lines)

    # ------------------------------------------------------------------ /btw
    _BTW_SYSTEM_PROMPT = "\n".join([
        "You are answering an ephemeral /btw side question about the current conversation.",
        "Use the conversation only as background context.",
        "Answer only the side question in the last user message.",
        "Do not continue, resume, or complete any unfinished task from the conversation.",
        "Do not emit tool calls, pseudo-tool calls, shell commands, file writes, patches, or "
        "code unless the side question explicitly asks for them.",
        "Do not say you will continue the main task after answering.",
        "If the question can be answered briefly, answer briefly.",
    ])

    async def cmd_btw(args: str, ctx: CommandContext) -> str:
        """Ask an ephemeral side question without affecting the main conversation.

        Snapshots the current session's message history (tool calls/results
        stripped), runs a one-shot LLM call with a BTW-specific system prompt,
        and returns the answer.  The main session's history is never touched.

        Usage: /btw <question>
        """
        question = args.strip()
        if not question:
            return "Usage: /btw <question>"

        from pyclopse.agents.runner import AgentRunner, _strip_tool_machinery

        # Determine model + config from the active runner (or agent config fallback)
        main_runner = _get_runner(ctx)
        history_snapshot: list = []
        model = "sonnet"
        pyclopse_config = None

        if main_runner is not None:
            model = main_runner.model
            pyclopse_config = getattr(main_runner, "pyclopse_config", None)
            if getattr(main_runner, "_app", None) is not None:
                try:
                    fa_agent = main_runner._app._agent(None)
                    raw = list(getattr(fa_agent, "message_history", []))
                    history_snapshot = _strip_tool_machinery(raw)
                except Exception as _e:
                    logger.debug(f"/btw: could not snapshot history: {_e}")
        elif ctx.session and getattr(ctx.gateway, "_agent_manager", None):
            _ag = ctx.gateway._agent_manager.get_agent(ctx.session.agent_id)
            if _ag:
                model = getattr(_ag.config, "model", model) or model
                pyclopse_config = getattr(_ag, "pyclopse_config", None)

        btw_prompt = "\n".join([
            "Answer this side question only.",
            "Ignore any unfinished task in the conversation while answering it.",
            "",
            "<btw_side_question>",
            question,
            "</btw_side_question>",
        ])

        # Ephemeral runner: BTW system prompt, same model, no MCP tools, no history file
        ephemeral = AgentRunner(
            agent_name="btw",
            instruction=_BTW_SYSTEM_PROMPT,
            model=model,
            servers=[],          # no tools — BTW is a pure LLM call
            history_path=None,   # never saved to disk
            max_iterations=1,
            pyclopse_config=pyclopse_config,
        )
        try:
            await ephemeral.initialize()

            # Inject the cleaned history snapshot directly into the FA agent
            # so the LLM has context without tool-call noise.
            if history_snapshot:
                try:
                    fa_btw = ephemeral._app._agent(None)
                    fa_btw.message_history[:] = history_snapshot
                except Exception as _e:
                    logger.debug(f"/btw: could not inject history: {_e}")

            response = await ephemeral.run(btw_prompt)
            return response
        except Exception as e:
            logger.warning(f"/btw failed: {e}", exc_info=True)
            return f"[/btw error: {e}]"
        finally:
            try:
                await ephemeral.cleanup()
            except Exception:
                pass

    # ---------------------------------------------------------------- register
    registry.register("start",   cmd_start,   "Start / show welcome message",               usage="/start")
    registry.register("help",    cmd_help,    "Show available commands",                     usage="/help")
    registry.register("new",     cmd_new,     "Start a new session",                         usage="/new")
    registry.register("reset",   cmd_reset,   "Clear session history",                       usage="/reset")
    registry.register("stop",    cmd_stop,    "Cancel the current running request",          usage="/stop")
    registry.register("compact", cmd_compact, "Compress session context",                    usage="/compact [instructions]")
    registry.register("btw",     cmd_btw,     "Ask an ephemeral side question (no history impact)", usage="/btw <question>")
    registry.register("status",  cmd_status,  "Show gateway status",                         usage="/status")
    registry.register("whoami",  cmd_whoami,  "Show your sender ID and session info",        usage="/whoami")
    registry.register("model",   cmd_model,   "Show or set the model for this session",      usage="/model [model-name]")
    registry.register("models",  cmd_models,  "List configured model providers",             usage="/models")
    registry.register("think",   cmd_think,   "Set thinking budget (off/low/medium/high/max)", usage="/think [level]")
    registry.register("usage",   cmd_usage,   "Show message counts and uptime",              usage="/usage")
    registry.register("context", cmd_context, "Show current session context",                usage="/context")
    registry.register("reload",  cmd_reload,  "Reload config from disk",                     usage="/reload")
    registry.register("restart", cmd_restart, "Reload config and reinitialize gateway",      usage="/restart")
    registry.register("config",  cmd_config,  "Show or get config values",                   usage="/config [show|get <key>]")
    registry.register("export",  cmd_export,  "Export session history to a file",            usage="/export [path]")
    registry.register("verbose", cmd_verbose, "Toggle verbose output for this session",      usage="/verbose [on|off]")
    registry.register("approve", cmd_approve, "Manage exec approval allowlist",              usage="/approve [list|add|remove]")
    registry.register("reboot",  cmd_reboot,  "Hard-restart the process (picks up code changes)", usage="/reboot")
    registry.register("tts",     cmd_tts,     "Toggle TTS output",                           usage="/tts [on|off|status]")
    registry.register("job",     cmd_job,     "Manage scheduled jobs",                       usage="/job [list|add|del|run|help]")
    registry.register("skills",    cmd_skills,    "List available skills",                        usage="/skills")
    registry.register("skill",    cmd_skill,     "Invoke a skill by name",                       usage="/skill <name> [args]")
    registry.register("subagents", cmd_subagents, "Manage background subagents",                 usage="/subagents [list|status|kill|interrupt|send]")
    registry.register("queue",    cmd_queue,    "Show or set message queue mode",               usage="/queue [mode=<mode>] [reset]")
    registry.register("history",   cmd_history,   "Show, save or load session history (FA ACP)",   usage="/history [show|save [name]|load [name]]")
    registry.register("clear",     cmd_clear,     "Clear history or undo last turn (FA ACP)",      usage="/clear [last]")
    registry.register("mcp",       cmd_mcp,       "Manage MCP servers at runtime (FA ACP)",        usage="/mcp [list|add|remove]")
    registry.register("cards",     cmd_cards,     "List agent cards (FA ACP)",                     usage="/cards")
    registry.register("card",      cmd_card,      "Inspect an agent card (FA ACP)",                usage="/card <name>")
    registry.register("agent",     cmd_agent,     "Agent introspection (FA ACP)",                  usage="/agent [name]")
    registry.register("bash",      cmd_bash,      "Run a shell command and send output to agent",  usage="/bash <command>")
    registry.register("allowlist", cmd_allowlist, "Manage channel allowlist at runtime",           usage="/allowlist [list|add <id>|remove <id>]")
    registry.register("reasoning",  cmd_reasoning,  "Toggle reasoning output visibility",              usage="/reasoning [on|off|status]")
    registry.register("session",    cmd_session,    "Show or configure the current session",           usage="/session [timeout <min>|window <n>]")
    registry.register("commands",   cmd_commands,   "Show all commands in compact format",             usage="/commands")
    registry.register("debug",      cmd_debug,      "Set or inspect runtime debug flags",              usage="/debug [set <k> <v>|unset <k>|reset]")
    registry.register("activation", cmd_activation, "Set group activation mode (always/mention)",      usage="/activation [always|mention]")
    registry.register("elevated",   cmd_elevated,   "Toggle elevated exec approval for this session",  usage="/elevated [on|off|ask|full]")
    registry.register("acp",        cmd_acp,        "ACP session management (FA ACP pass-through)",    usage="/acp [spawn|cancel|steer|close|sessions|...]")
    registry.register("exec",       cmd_exec,       "Set per-session exec host/level/ask defaults",    usage="/exec [host|level|ask|reset]")
    registry.register("send",       cmd_send,       "Set outbound send policy for this session",       usage="/send [on|off|inherit]")
    registry.register("focus",      cmd_focus,      "Bind current thread/topic to an agent",           usage="/focus [agent_id]")
    registry.register("unfocus",    cmd_unfocus,    "Remove thread/topic binding",                     usage="/unfocus")
    registry.register("agents",     cmd_agents,     "List thread/topic bindings for this channel",     usage="/agents")
    registry.register("memories",   cmd_memories,   "List or search vault memory facts",               usage="/memories [query] [--all] [--type <t>]")
    registry.register("forget",     cmd_forget,     "Archive a vault memory fact by ID",               usage="/forget <fact-id>")
    registry.register("ingest",     cmd_ingest,     "Bulk-ingest session history and memory into vault", usage="/ingest [sessions|memory]")
