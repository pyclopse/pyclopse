"""REST endpoints for slash commands — used by remote TUI."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("pyclopse.api.commands")


def _get_gateway():
    from pyclopse.api.app import get_gateway
    return get_gateway()


@router.get("/")
async def list_commands():
    """List all registered slash commands."""
    gw = _get_gateway()
    registry = getattr(gw, "_command_registry", None)
    if not registry:
        return {"commands": []}
    return {
        "commands": [
            {"name": cmd.name, "description": cmd.description}
            for cmd in sorted(registry._commands.values(), key=lambda c: c.name)
        ]
    }


class DispatchRequest(BaseModel):
    command: str
    agent_id: str


@router.post("/dispatch")
async def dispatch_command(request: DispatchRequest):
    """Dispatch a slash command and return its result."""
    gw = _get_gateway()
    from pyclopse.core.commands import CommandContext

    session = None
    sm = getattr(gw, "session_manager", None)
    if sm and request.agent_id:
        try:
            session = await sm.get_or_create_session(
                agent_id=request.agent_id,
                channel="tui",
                user_id="tui_user",
            )
        except Exception:
            pass

    ctx = CommandContext(
        gateway=gw,
        session=session,
        sender_id="tui_user",
        channel="tui",
    )
    result = await gw._command_registry.dispatch(request.command, ctx)
    return {"result": result}
