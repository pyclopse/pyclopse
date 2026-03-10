"""Hooks inspection API routes."""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger("pyclaw.api.hooks")

router = APIRouter()


def _get_gateway():
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_hooks():
    """
    List all registered hook handlers grouped by event.

    Returns each event with its handlers (name, priority, source, description).
    If the hook registry has not been initialised (gateway not started) an
    empty result is returned rather than an error.
    """
    gateway = _get_gateway()
    registry = getattr(gateway, "_hook_registry", None)

    if registry is None:
        return {"events": {}, "total_events": 0, "total_handlers": 0}

    hooks = registry.list_hooks()
    return {
        "events": hooks,
        "total_events": registry.event_count(),
        "total_handlers": registry.handler_count(),
    }
