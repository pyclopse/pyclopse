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
    """Recursively redact sensitive keys from a dict/list structure.

    Keys listed in ``_REDACTED_KEYS`` are replaced with ``"***REDACTED***"``
    at any nesting level.

    Args:
        obj (Any): The data structure to walk (dict, list, or scalar).
        depth (int): Current recursion depth; recursion stops at 10.

    Returns:
        Any: A new data structure with sensitive values replaced.
    """
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
    """Retrieve the global gateway instance.

    Returns:
        Any: The gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
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


@router.get("/secrets", response_model=Dict[str, Any])
async def list_secrets():
    """
    List all registered secret names and their source types.

    Never returns actual secret values — use GET /secrets/{name} for that.
    """
    from pyclaw.config.loader import load_secrets_registry
    from pyclaw.secrets.manager import SecretsManager

    manager = SecretsManager(load_secrets_registry())
    entries = []
    for name in manager.registered_names():
        defn = manager._parsed.get(name)
        entry: Dict[str, Any] = {"name": name, "source": getattr(defn, "source", "unknown")}
        if hasattr(defn, "var") and defn.var:
            entry["var"] = defn.var
        if hasattr(defn, "path"):
            entry["path"] = defn.path
        if hasattr(defn, "account"):
            entry["account"] = defn.account
        entries.append(entry)

    return {"secrets": entries, "count": len(entries)}


@router.get("/secrets/{name}", response_model=Dict[str, Any])
async def get_secret(name: str):
    """
    Resolve a named secret via the pyclaw secrets manager.

    The secret must be registered in ~/.pyclaw/secrets/secrets.yaml.
    """
    from pyclaw.config.loader import load_secrets_registry
    from pyclaw.secrets.manager import SecretsManager, ResolutionError

    manager = SecretsManager(load_secrets_registry())
    try:
        value = manager.resolve_name(name)
        return {"name": name, "value": value}
    except ResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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
