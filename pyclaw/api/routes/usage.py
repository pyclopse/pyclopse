"""Usage statistics API routes."""

import time
import logging
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("pyclaw.api.usage")

router = APIRouter()


def _get_gateway():
    """Retrieve the global gateway instance.

    Returns:
        Gateway: The active gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_usage():
    """Return usage statistics: message counts, uptime, per-agent/channel breakdown.

    Returns:
        Dict[str, Any]: Usage snapshot containing:
            - ``messages_total`` (int): Total messages processed since startup.
            - ``messages_by_agent`` (Dict[str, int]): Per-agent message counts.
            - ``messages_by_channel`` (Dict[str, int]): Per-channel message counts.
            - ``uptime_seconds`` (float): Seconds since the gateway started.
            - ``started_at`` (float): Unix timestamp of gateway startup.
    """
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
