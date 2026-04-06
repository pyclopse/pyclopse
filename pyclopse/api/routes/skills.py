"""Skills API routes — cache management and discovery endpoints."""

import logging
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("pyclopse.api.skills")

router = APIRouter()


@router.post("/reload", response_model=Dict[str, Any])
async def reload_skills_cache():
    """Invalidate the in-process skills discovery cache.

    Forces a fresh filesystem scan on the next call to any skills discovery
    function (``discover_skills``, ``find_skill``).  Skills are normally cached
    for 1 hour; call this endpoint after creating or modifying skill directories
    to pick up changes immediately.

    Returns:
        Dict[str, Any]: ``{"status": "ok", "message": "..."}`` on success.
    """
    from pyclopse.skills.registry import invalidate_skills_cache
    invalidate_skills_cache()
    logger.info("Skills cache invalidated via REST API")
    return {
        "status": "ok",
        "message": "Skills cache cleared. Next discovery call will perform a fresh scan.",
    }
