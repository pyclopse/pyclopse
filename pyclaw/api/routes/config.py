"""Config API routes — view and reload gateway configuration."""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("pyclaw.api.config")

router = APIRouter()

# Fields that must never appear in the GET response
_REDACTED_KEYS = frozenset({
    "api_key", "apiKey", "bot_token", "botToken",
    "access_token", "accessToken", "signing_secret", "signingSecret",
    "secret_key", "secretKey", "token",
})


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive keys from a dict/list structure."""
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if k in _REDACTED_KEYS else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj


def _get_gateway():
    from pyclaw.api.app import get_gateway
    return get_gateway()


@router.get("/", response_model=Dict[str, Any])
async def get_config():
    """Return the current gateway configuration (sensitive values redacted)."""
    gateway = _get_gateway()
    try:
        raw = gateway.config.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")
    return {"config": _redact(raw)}


@router.post("/reload", response_model=Dict[str, Any])
async def reload_config():
    """Reload configuration from disk and apply non-destructive changes."""
    gateway = _get_gateway()
    try:
        changed = await gateway.reload_config()
    except Exception as e:
        logger.error(f"Config reload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Config reload failed: {e}")
    return {"reloaded": True, "changed": changed}
