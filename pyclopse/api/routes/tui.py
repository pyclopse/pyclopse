"""REST endpoints specifically for TUI dashboard support.

These supplement the existing agents/sessions/jobs routes with data
the TUI needs that isn't covered elsewhere.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("pyclopse.api.tui")

_AGENTS_DIR = Path("~/.pyclopse/agents").expanduser()
_BROWSER_EXCLUDE_DIRS = {"sessions", "runs", "__pycache__", ".git", "logs"}
_BROWSER_EXCLUDE_EXTS = {".pyc", ".pyo", ".so", ".dylib", ".jsonl"}


def _get_gateway():
    from pyclopse.api.app import get_gateway
    return get_gateway()


# ── Agent-specific endpoints ─────────────────────────────────────────────────

@router.get("/agents/{agent_id}/config")
async def get_agent_config(agent_id: str):
    """Return full agent config as field/value pairs."""
    gw = _get_gateway()
    am = getattr(gw, "_agent_manager", None)
    if not am:
        raise HTTPException(404, "Agent manager not available")
    agent = am.agents.get(agent_id)
    if not agent or not agent.config:
        raise HTTPException(404, f"Agent '{agent_id}' not found")
    cfg = agent.config
    result: dict[str, Any] = {
        "name": cfg.name,
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "context_window": cfg.context_window,
        "show_thinking": cfg.show_thinking,
        "use_fastagent": cfg.use_fastagent,
    }
    return result


@router.get("/agents/{agent_id}/system-prompt")
async def get_system_prompt(agent_id: str):
    """Return the reconstructed system prompt for an agent."""
    try:
        from pyclopse.core.prompt_builder import build_system_prompt
        text = build_system_prompt(agent_name=agent_id, config_dir="~/.pyclopse")
        return {"agent_id": agent_id, "prompt": text, "length": len(text)}
    except Exception as e:
        raise HTTPException(500, f"Error building system prompt: {e}")


@router.get("/agents/{agent_id}/log-tail")
async def get_agent_log_tail(agent_id: str, lines: int = 500):
    """Return the last N lines of the agent log."""
    log_path = _AGENTS_DIR / agent_id / "logs" / "agent.log"
    if not log_path.exists():
        return {"agent_id": agent_id, "content": "", "exists": False}
    all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    content = "\n".join(all_lines[-lines:])
    return {"agent_id": agent_id, "content": content, "exists": True, "total_lines": len(all_lines)}


@router.get("/agents/{agent_id}/active-session")
async def get_active_session(agent_id: str):
    """Return the active session for an agent with its history."""
    gw = _get_gateway()
    sm = getattr(gw, "session_manager", None)
    if not sm:
        raise HTTPException(503, "Session manager not available")
    session = await sm.get_active_session(agent_id)
    if not session:
        return {"agent_id": agent_id, "session": None}
    history = ""
    if session.history_path and session.history_path.exists():
        history = session.history_path.read_text(encoding="utf-8", errors="replace")
    return {
        "agent_id": agent_id,
        "session": {"id": session.id, "history": history},
    }


@router.get("/agents/{agent_id}/show-thinking")
async def get_show_thinking(agent_id: str):
    """Return whether an agent shows thinking blocks."""
    gw = _get_gateway()
    am = getattr(gw, "_agent_manager", None)
    if not am:
        return {"show_thinking": False}
    agent = am.agents.get(agent_id)
    if not agent:
        return {"show_thinking": False}
    runner = getattr(agent, "fast_agent_runner", None)
    return {"show_thinking": bool(getattr(runner, "show_thinking", False))}


# ── File browser ─────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/files")
async def list_agent_files(agent_id: str):
    """List files in the agent's data directory."""
    agent_dir = _AGENTS_DIR / agent_id
    files = []
    if not agent_dir.exists():
        return {"agent_id": agent_id, "files": files}
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
    return {"agent_id": agent_id, "files": files}


@router.get("/agents/{agent_id}/files/content")
async def read_agent_file(agent_id: str, path: str):
    """Read a file from the agent's data directory."""
    file_path = (_AGENTS_DIR / agent_id / path).resolve()
    # Path traversal protection
    if not str(file_path).startswith(str(_AGENTS_DIR / agent_id)):
        raise HTTPException(403, "Path traversal not allowed")
    if not file_path.exists():
        raise HTTPException(404, f"File not found: {path}")
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "content": content}


class WriteFileRequest(BaseModel):
    content: str


@router.put("/agents/{agent_id}/files/content")
async def write_agent_file(agent_id: str, path: str, request: WriteFileRequest):
    """Write a file to the agent's data directory."""
    file_path = (_AGENTS_DIR / agent_id / path).resolve()
    if not str(file_path).startswith(str(_AGENTS_DIR / agent_id)):
        raise HTTPException(403, "Path traversal not allowed")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(request.content, encoding="utf-8")
    return {"path": path, "written": True}


# ── Skills ───────────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/skills")
async def list_skills(agent_id: str):
    """List skills available to an agent."""
    gw = _get_gateway()
    from pyclopse.skills.registry import discover_skills
    am = getattr(gw, "_agent_manager", None)
    gw_dirs: list[str] = []
    agent_dirs: list[str] = []
    if am:
        agent = am.agents.get(agent_id)
        if agent:
            pc = getattr(agent, "pyclopse_config", None)
            gw_cfg = getattr(pc, "gateway", None) if pc else None
            gw_dirs = list(getattr(gw_cfg, "skills_dirs", None) or [])
            agent_dirs = list(getattr(agent.config, "skills_dirs", None) or [])
    skills = discover_skills(
        agent_name=agent_id,
        config_dir="~/.pyclopse",
        extra_dirs=(gw_dirs + agent_dirs) or None,
    )
    return {
        "agent_id": agent_id,
        "skills": [
            {
                "name": s.name,
                "version": s.version or "",
                "allowed_tools": " ".join(s.allowed_tools) if s.allowed_tools else "",
                "description": (s.description or "")[:80],
            }
            for s in sorted(skills, key=lambda s: s.name.lower())
        ],
    }


@router.get("/agents/{agent_id}/skills/{skill_name}/content")
async def get_skill_content(agent_id: str, skill_name: str):
    """Return SKILL.md content for a skill."""
    from pyclopse.skills.registry import find_skill
    skill = find_skill(skill_name, agent_name=agent_id, config_dir="~/.pyclopse")
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"name": skill_name, "content": skill.read_content()}


# ── A2A ──────────────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/a2a-card")
async def get_agent_card(agent_id: str):
    """Return A2A agent card for an agent."""
    gw = _get_gateway()
    from pyclopse.tui.dashboard import _build_a2a_card
    return _build_a2a_card(agent_id, gw)
