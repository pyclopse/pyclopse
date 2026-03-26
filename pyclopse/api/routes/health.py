"""Detailed health check API routes."""

import time
import logging
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("pyclopse.api.health")

router = APIRouter()


def _get_gateway():
    """Retrieve the global gateway instance.

    Returns:
        Any: The gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclopse.api.app import get_gateway
    return get_gateway()


@router.get("/detail", response_model=Dict[str, Any])
async def health_detail():
    """Return extended health information about the gateway and its subsystems.

    Inspects each major subsystem (session manager, agent manager, job
    scheduler, Telegram, audit logger) and reports its individual status.
    Uptime is derived from ``gateway._usage["started_at"]``.

    Returns:
        Dict[str, Any]: Health report with keys:
            - ``status`` ("healthy" | "degraded")
            - ``initialized`` (bool)
            - ``running`` (bool)
            - ``uptime_seconds`` (float)
            - ``subsystems`` (dict of per-subsystem status dicts)
    """
    gateway = _get_gateway()

    subsystems: Dict[str, Any] = {}

    # Session manager
    sm = getattr(gateway, "_session_manager", None)
    if sm is not None:
        try:
            sessions = sm.list_sessions()
            subsystems["session_manager"] = {
                "status": "ok",
                "active_sessions": len(sessions),
            }
        except Exception as e:
            subsystems["session_manager"] = {"status": "error", "detail": str(e)}
    else:
        subsystems["session_manager"] = {"status": "not_started"}

    # Agent manager
    am = getattr(gateway, "_agent_manager", None)
    if am is not None:
        agent_ids = list(am.agents.keys()) if am.agents else []
        subsystems["agent_manager"] = {
            "status": "ok",
            "agents": agent_ids,
            "agent_count": len(agent_ids),
        }
    else:
        subsystems["agent_manager"] = {"status": "not_started"}

    # Job scheduler
    js = getattr(gateway, "_job_scheduler", None)
    if js is not None:
        try:
            job_count = len(js.list_jobs()) if hasattr(js, "list_jobs") else 0
            subsystems["job_scheduler"] = {"status": "ok", "jobs": job_count}
        except Exception as e:
            subsystems["job_scheduler"] = {"status": "error", "detail": str(e)}
    else:
        subsystems["job_scheduler"] = {"status": "not_configured"}

    # Telegram channel
    tg_bot = getattr(gateway, "_telegram_bot", None)
    subsystems["telegram"] = {
        "status": "connected" if tg_bot is not None else "not_configured"
    }

    # Audit logger
    al = getattr(gateway, "_audit_logger", None)
    subsystems["audit_logger"] = {
        "status": "ok" if al is not None else "disabled"
    }

    # Is initialized
    is_initialized = getattr(gateway, "_initialized", False)
    is_running = getattr(gateway, "_is_running", False)

    usage = getattr(gateway, "_usage", {})
    started_at = usage.get("started_at", time.time())
    uptime_seconds = round(time.time() - started_at, 1)

    overall = "healthy" if is_initialized else "degraded"

    return {
        "status": overall,
        "initialized": is_initialized,
        "running": is_running,
        "uptime_seconds": uptime_seconds,
        "subsystems": subsystems,
    }
