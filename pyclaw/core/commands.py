"""Slash command registry for pyclaw gateway."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("pyclaw.commands")


@dataclass
class CommandContext:
    """Context passed to every command handler."""

    gateway: Any          # Gateway instance
    session: Optional[Any]  # Session (may be None for stateless commands)
    sender_id: str
    channel: str


@dataclass
class Command:
    """A registered slash command."""

    name: str
    description: str
    usage: str
    handler: Callable  # async (args: str, ctx: CommandContext) -> str


class CommandRegistry:
    """Registry of slash commands available in pyclaw."""

    def __init__(self) -> None:
        self._commands: Dict[str, Command] = {}

    def register(
        self,
        name: str,
        handler: Callable,
        description: str,
        usage: str = "",
    ) -> None:
        """Register a slash command handler."""
        key = name.lstrip("/").lower()
        self._commands[key] = Command(
            name=key,
            description=description,
            usage=usage or f"/{key}",
            handler=handler,
        )

    async def dispatch(self, text: str, ctx: CommandContext) -> Optional[str]:
        """Dispatch a slash command.

        Returns None if *text* does not start with '/'.
        Returns a reply string for any recognised or unrecognised command.
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
        """Return a human-readable list of all registered commands."""
        if not self._commands:
            return "No commands registered."
        lines = ["Available commands:"]
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            lines.append(f"  /{cmd.name} — {cmd.description}")
        return "\n".join(lines)

    def commands_for_telegram(self) -> List[tuple]:
        """Return (command, description) pairs suitable for Telegram setMyCommands.
        Telegram requires descriptions 1-256 chars and command names 1-32 chars, lowercase."""
        result = []
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            name = cmd.name[:32].lower()
            desc = (cmd.description or cmd.name)[:256] or name
            result.append((name, desc))
        return result


# ---------------------------------------------------------------------------
# Built-in command handlers
# ---------------------------------------------------------------------------


def register_builtin_commands(registry: CommandRegistry, gateway: Any) -> None:
    """Register the standard gateway commands into *registry*."""

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
            stamp = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
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
        return "✅ Session history cleared."

    async def cmd_status(args: str, ctx: CommandContext) -> str:
        status = ctx.gateway.get_status()
        agents = status.get("agents", {})
        sessions = status.get("sessions", {})
        jobs = status.get("jobs", {})
        lines = [
            "🟢 pyclaw Gateway",
            f"Running: {status.get('is_running', False)}",
            f"Config version: {status.get('config_version', '?')}",
            f"Agents: {agents.get('total_agents', 0)} "
            f"({agents.get('running_agents', 0)} running)",
            f"Sessions: {sessions.get('active_sessions', 0)} active "
            f"/ {sessions.get('total_sessions', 0)} total",
            f"Jobs: {jobs.get('total', 0)} total "
            f"/ {jobs.get('running', 0)} running",
        ]
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
            "👋 Hello! I'm your pyclaw assistant.\n\n"
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
        # Evict per-session runner so a fresh FastAgent context is created next turn
        if agent:
            await agent.evict_session_runner(ctx.session.id)
        ctx.session.context.clear()
        return f"✅ New session started (was {old_id}…). History cleared."

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
        """List configured model providers and available models."""
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
        """Show message counts and uptime for this session and gateway."""
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
        cfg_path = Path("~/.pyclaw/config/pyclaw.yaml").expanduser()
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
            return "⚠️ Live config edits are not yet supported via command. Edit ~/.pyclaw/config/pyclaw.yaml and use /reload."

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
            from pyclaw.skills.registry import discover_skills
            agent_id = ctx.session.agent_id if ctx.session else None
            skills = discover_skills(agent_name=agent_id)
            if not skills:
                return "No skills installed. Add skill directories to ~/.pyclaw/skills/."
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
            from pyclaw.skills.registry import find_skill
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

            from pyclaw.core.router import IncomingMessage
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
        """Hard-restart the pyclaw process (picks up code + config changes)."""
        import asyncio
        import os
        import sys

        async def _do_reboot():
            await asyncio.sleep(0.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.create_task(_do_reboot())
        return "🔄 Rebooting pyclaw… (back in a few seconds)"

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

    # ---------------------------------------------------------------- register
    registry.register("start",   cmd_start,   "Start / show welcome message",               usage="/start")
    registry.register("help",    cmd_help,    "Show available commands",                     usage="/help")
    registry.register("new",     cmd_new,     "Start a new session",                         usage="/new")
    registry.register("reset",   cmd_reset,   "Clear session history",                       usage="/reset")
    registry.register("stop",    cmd_stop,    "Cancel the current running request",          usage="/stop")
    registry.register("compact", cmd_compact, "Compress session context",                    usage="/compact [instructions]")
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
    registry.register("skills",  cmd_skills,  "List available skills",                        usage="/skills")
    registry.register("skill",   cmd_skill,   "Invoke a skill by name",                       usage="/skill <name> [args]")
