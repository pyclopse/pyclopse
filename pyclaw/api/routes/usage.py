"""Usage statistics API routes."""

import time
import logging
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("pyclaw.api.usage")

router = APIRouter()


def _get_gateway():
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_usage():
    """Return usage statistics: message counts, uptime, per-agent/channel breakdown."""
    gateway = _get_gateway()
    usage = gateway._usage
    started_at = usage.get("started_at", time.time())
    uptime_seconds = time.time() - started_at
    return {
        "messages_total": usage.get("messages_total", 0),
        "messages_by_agent": dict(usage.get("messages_by_agent", {})),
        "messages_by_channel": dict(usage.get("messages_by_channel", {})),
        "uptime_seconds": round(uptime_seconds, 1),
        "started_at": started_at,
    }
