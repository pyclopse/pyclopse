"""EmbeddedGatewayClient — wraps a live in-process Gateway object.

Every method here mirrors what the TUI views used to do via getattr() on
the gateway. This is a mechanical extraction that preserves identical behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("pyclopse.tui.embedded_client")

# Directories under agent dirs to skip in file listing
_BROWSER_EXCLUDE_DIRS = {"sessions", "runs", "__pycache__", ".git", "logs"}
_BROWSER_EXCLUDE_EXTS = {".pyc", ".pyo", ".so", ".dylib", ".jsonl"}


class EmbeddedGatewayClient:
    """In-process client that delegates directly to a live Gateway instance."""

    def __init__(self, gateway: Any) -> None:
        self._gw = gateway

    # ── Agents ────────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict[str, Any]]:
        am = getattr(self._gw, "_agent_manager", None)
        if not am:
            return []
        result = []
        for aid, agent in am.agents.items():
            cfg = agent.config
            result.append({
                "id": aid,
                "name": cfg.name if cfg else aid,
                "model": cfg.model if cfg else "",
            })
        return result

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        am = getattr(self._gw, "_agent_manager", None)
        if not am:
            return {}
        agent = am.agents.get(agent_id)
        if not agent or not agent.config:
            return {}
        cfg = agent.config
        rows: dict[str, Any] = {
            "name": cfg.name,
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "context_window": cfg.context_window,
            "max_iterations": cfg.max_iterations,
            "parallel_tool_calls": getattr(cfg, "parallel_tool_calls", None),
            "streaming_timeout": getattr(cfg, "streaming_timeout", None),
            "reasoning_effort": getattr(cfg, "reasoning_effort", None),
            "text_verbosity": getattr(cfg, "text_verbosity", None),
            "service_tier": getattr(cfg, "service_tier", None),
            "show_thinking": cfg.show_thinking,
            "typing_mode": getattr(cfg, "typing_mode", ""),
            "use_fastagent": cfg.use_fastagent,
            "workflow": getattr(cfg, "workflow", None),
        }
        q_cfg = getattr(cfg, "queue", None)
        if q_cfg:
            rows["queue.mode"] = str(getattr(q_cfg, "mode", ""))
            rows["queue.debounce_ms"] = getattr(q_cfg, "debounce_ms", "")
            rows["queue.cap"] = getattr(q_cfg, "cap", "")
        rows["fallbacks"] = ", ".join(getattr(cfg, "fallbacks", []) or [])
        tools_cfg = getattr(cfg, "tools", None)
        if tools_cfg:
            rows["tools.profile"] = str(getattr(tools_cfg, "profile", ""))
        rp = getattr(cfg, "request_params", None)
        if rp:
            for k, v in rp.items():
                rows[f"request_params.{k}"] = v
        return rows

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        sm = getattr(self._gw, "_session_manager", None)
        am = getattr(self._gw, "_agent_manager", None)
        context_window = None
        if am:
            agent = am.agents.get(agent_id)
            if agent and agent.config:
                context_window = getattr(agent.config, "context_window", None)
        if not sm:
            return []
        sessions = sm.list_sessions_sync(agent_id=agent_id)
        result = []
        for s in sessions:
            ctx = getattr(s, "context", {}) or {}
            ctx_tokens = ctx.get("_ctx_tokens", 0) or 0
            result.append({
                "id": s.id,
                "channel": s.channel or "",
                "user_id": str(s.user_id or ""),
                "message_count": s.message_count,
                "context_window": context_window,
                "ctx_tokens": ctx_tokens,
                "updated_at": s.updated_at.isoformat() if s.updated_at else "",
                "is_active": getattr(s, "is_active", False),
            })
        return result

    async def get_active_session(self, agent_id: str) -> dict[str, Any] | None:
        sm = getattr(self._gw, "session_manager", None)
        if not sm:
            return None
        session = await sm.get_active_session(agent_id)
        if not session:
            return None
        history = ""
        if session.history_path and session.history_path.exists():
            history = session.history_path.read_text(encoding="utf-8", errors="replace")
        return {"id": session.id, "history": history}

    async def get_session_history(self, agent_id: str, session_id: str) -> str:
        sm = getattr(self._gw, "_session_manager", None)
        if not sm:
            return ""
        sessions = sm.list_sessions_sync(agent_id=agent_id)
        session = next((s for s in sessions if s.id == session_id), None)
        if session and session.history_path and session.history_path.exists():
            raw = session.history_path.read_text()
            data = json.loads(raw)
            return json.dumps(data, indent=2)
        return ""

    # ── Messages ──────────────────────────────────────────────────────────────

    async def send_message(self, agent_id: str, content: str) -> None:
        await self._gw.handle_message(
            channel="tui",
            sender="tui_user",
            sender_id="tui_user",
            content=content,
            agent_id=agent_id,
        )

    async def dispatch_command(self, agent_id: str, command: str) -> str | None:
        from pyclopse.core.commands import CommandContext
        session = None
        sm = getattr(self._gw, "session_manager", None)
        if sm and agent_id:
            try:
                session = await sm.get_or_create_session(
                    agent_id=agent_id,
                    channel="tui",
                    user_id="tui_user",
                )
            except Exception:
                pass
        ctx = CommandContext(
            gateway=self._gw,
            session=session,
            sender_id="tui_user",
            channel="tui",
        )
        return await self._gw._command_registry.dispatch(command, ctx)

    async def list_commands(self) -> list[dict[str, str]]:
        registry = getattr(self._gw, "_command_registry", None)
        if not registry:
            return []
        return [
            {"name": cmd.name, "description": cmd.description}
            for cmd in sorted(registry._commands.values(), key=lambda c: c.name)
        ]

    # ── Events ────────────────────────────────────────────────────────────────

    def subscribe_events(self, agent_id: str) -> asyncio.Queue:
        return self._gw.subscribe_agent(agent_id)

    def unsubscribe_events(self, agent_id: str, queue: asyncio.Queue) -> None:
        self._gw.unsubscribe_agent(agent_id, queue)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        js = getattr(self._gw, "_job_scheduler", None)
        if not js:
            return []
        job_agent_map: dict = getattr(js, "_job_agents", {})
        result = []
        for job_id, job in js.jobs.items():
            if job_agent_map.get(job_id) != agent_id:
                continue
            sched = getattr(job, "schedule", None)
            if sched:
                kind = getattr(sched, "kind", "")
                if kind == "cron":
                    schedule_str = f"cron: {getattr(sched, 'expr', '')}"
                elif kind == "interval":
                    schedule_str = f"every {getattr(sched, 'seconds', 0)}s"
                else:
                    schedule_str = str(sched)
            else:
                schedule_str = ""
            running = getattr(js, "_running_jobs", set())
            result.append({
                "id": job.id,
                "name": job.name or job.id,
                "schedule": schedule_str,
                "enabled": getattr(job, "enabled", True),
                "status": "running" if job.id in running else "idle",
                "next_run": job.next_run.isoformat() if getattr(job, "next_run", None) else "",
                "last_run": job.last_run.isoformat() if getattr(job, "last_run", None) else "",
                "run_count": getattr(job, "run_count", 0),
            })
        return result

    async def run_job(self, job_id: str) -> None:
        js = getattr(self._gw, "_job_scheduler", None)
        if js:
            await js.run_job_now(job_id)

    async def get_job_runs(self, agent_id: str, job_id: str) -> list[dict[str, Any]]:
        runs_file = (
            Path("~/.pyclopse/agents").expanduser()
            / agent_id / "runs" / f"{job_id}.jsonl"
        )
        runs = []
        if runs_file.exists():
            for line in reversed(runs_file.read_text().splitlines()):
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return runs

    # ── Status ────────────────────────────────────────────────────────────────

    async def get_usage(self) -> dict[str, Any]:
        return dict(getattr(self._gw, "_usage", {}) or {})

    async def get_system_prompt(self, agent_id: str) -> str:
        from pyclopse.core.prompt_builder import build_system_prompt
        return build_system_prompt(agent_name=agent_id, config_dir="~/.pyclopse")

    async def get_agent_log_tail(self, agent_id: str, lines: int = 500) -> str:
        log_path = (
            Path("~/.pyclopse/agents").expanduser() / agent_id / "logs" / "agent.log"
        )
        if not log_path.exists():
            return ""
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-lines:])

    # ── Files ─────────────────────────────────────────────────────────────────

    async def list_agent_files(self, agent_id: str) -> list[dict[str, str]]:
        agent_dir = Path("~/.pyclopse/agents").expanduser() / agent_id
        files = []
        if not agent_dir.exists():
            return files
        for p in sorted(agent_dir.rglob("*")):
            if not p.is_file():
                continue
            parts = p.relative_to(agent_dir).parts
            if any(part in _BROWSER_EXCLUDE_DIRS for part in parts):
                continue
            if p.suffix in _BROWSER_EXCLUDE_EXTS:
                continue
            stat = p.stat()
            files.append({
                "path": str(p.relative_to(agent_dir)),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
            })
        return files

    async def read_agent_file(self, agent_id: str, path: str) -> str:
        file_path = Path("~/.pyclopse/agents").expanduser() / agent_id / path
        if not file_path.exists():
            return ""
        return file_path.read_text(encoding="utf-8", errors="replace")

    async def write_agent_file(self, agent_id: str, path: str, content: str) -> None:
        file_path = Path("~/.pyclopse/agents").expanduser() / agent_id / path
        file_path.write_text(content, encoding="utf-8")

    # ── Skills ────────────────────────────────────────────────────────────────

    async def list_skills(self, agent_id: str) -> list[dict[str, Any]]:
        from pyclopse.skills.registry import discover_skills
        am = getattr(self._gw, "_agent_manager", None)
        gw_dirs: list[str] = []
        agent_dirs: list[str] = []
        if am:
            agent = am.agents.get(agent_id)
            if agent:
                pc = getattr(agent, "pyclopse_config", None)
                gw_cfg = getattr(pc, "gateway", None) if pc else None
                gw_dirs = list(getattr(gw_cfg, "skills_dirs", None) or [])
                agent_dirs = list(getattr(agent.config, "skills_dirs", None) or [])
        extra = gw_dirs + agent_dirs
        skills = discover_skills(
            agent_name=agent_id,
            config_dir="~/.pyclopse",
            extra_dirs=extra or None,
        )
        return [
            {
                "name": s.name,
                "version": s.version or "",
                "allowed_tools": " ".join(s.allowed_tools) if s.allowed_tools else "",
                "description": (s.description or "")[:80],
            }
            for s in sorted(skills, key=lambda s: s.name.lower())
        ]

    async def get_skill_content(self, agent_id: str, skill_name: str) -> str:
        from pyclopse.skills.registry import find_skill
        skill = find_skill(skill_name, agent_name=agent_id, config_dir="~/.pyclopse")
        return skill.read_content() if skill else ""

    # ── A2A ───────────────────────────────────────────────────────────────────

    async def get_agent_card(self, agent_id: str) -> dict[str, Any]:
        # Import the same builder function the TUI already used
        from pyclopse.tui.dashboard import _build_a2a_card
        return _build_a2a_card(agent_id, self._gw)

    # ── Misc ──────────────────────────────────────────────────────────────────

    async def get_show_thinking(self, agent_id: str) -> bool:
        am = getattr(self._gw, "_agent_manager", None)
        if not am:
            return False
        agent = am.agents.get(agent_id)
        if not agent:
            return False
        runner = getattr(agent, "fast_agent_runner", None)
        return bool(getattr(runner, "show_thinking", False))
