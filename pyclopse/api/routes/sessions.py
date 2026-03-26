"""Sessions API routes."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("pyclopse.api.sessions")

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class MessageOut(BaseModel):
    """A single message from a session's conversation history.

    Attributes:
        id (str): Synthetic message identifier (``{session_id}-{index}``).
        role (str): Speaker role ("user" or "assistant").
        content (str): Plain text content of the message.
        timestamp (str): ISO-formatted timestamp of the session creation.
    """

    id: str
    role: str
    content: str
    timestamp: str


class SessionSummary(BaseModel):
    """Summary information for a single session.

    Attributes:
        id (str): Unique session identifier.
        agent_id (str): ID of the agent that owns the session.
        channel (str): Channel the session belongs to.
        user_id (str): ID of the user the session is associated with.
        created_at (str): ISO-formatted creation timestamp.
        updated_at (str): ISO-formatted last-updated timestamp.
        message_count (int): Number of messages exchanged.
        is_active (bool): Whether the session is still active.
    """

    id: str
    agent_id: str
    channel: str
    user_id: str
    created_at: str
    updated_at: str
    message_count: int
    is_active: bool


class SessionDetail(SessionSummary):
    """Full session detail including conversation history.

    Extends :class:`SessionSummary` with the complete message log.

    Attributes:
        messages (List[MessageOut]): All messages in chronological order.
    """

    messages: List[MessageOut]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _session_manager():
    """Retrieve the gateway's session manager.

    Returns:
        Any: The session manager instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclopse.api.app import get_gateway
    return get_gateway().session_manager


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=Dict[str, Any])
async def list_sessions(
    agent_id: Optional[str] = None,
    channel: Optional[str] = None,
    user_id: Optional[str] = None,
    active_only: bool = True,
):
    """List sessions with optional filters.

    Args:
        agent_id (Optional[str]): Filter to sessions belonging to this agent.
        channel (Optional[str]): Filter to sessions on this channel.
        user_id (Optional[str]): Filter to sessions for this user.
        active_only (bool): When True, return only active sessions. Defaults to True.

    Returns:
        Dict[str, Any]: ``{"sessions": [...], "total": int}`` with each entry
            as a :class:`SessionSummary` dict.
    """
    sm = _session_manager()
    sessions = await sm.list_sessions(
        agent_id=agent_id,
        channel=channel,
        user_id=user_id,
        active_only=active_only,
    )
    return {
        "sessions": [
            SessionSummary(
                id=s.id,
                agent_id=s.agent_id,
                channel=s.channel,
                user_id=s.user_id,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
                message_count=s.message_count,
                is_active=s.is_active,
            ).model_dump()
            for s in sessions
        ],
        "total": len(sessions),
    }


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str):
    """Get a session by ID, including full message history."""
    sm = _session_manager()
    session = await sm.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    # Load conversation history from the FA native history file
    messages_out: List[MessageOut] = []
    if session.history_path and session.history_path.exists():
        try:
            from fast_agent.mcp.prompt_serialization import load_messages
            fa_msgs = load_messages(str(session.history_path))
            for i, msg in enumerate(fa_msgs):
                text = (msg.all_text() if hasattr(msg, "all_text") else None) or ""
                messages_out.append(
                    MessageOut(
                        id=f"{session.id}-{i}",
                        role=msg.role,
                        content=text,
                        timestamp=session.created_at.isoformat(),
                    )
                )
        except Exception as exc:
            logger.warning(f"Could not load history for session {session_id}: {exc}")

    return SessionDetail(
        id=session.id,
        agent_id=session.agent_id,
        channel=session.channel,
        user_id=session.user_id,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        message_count=session.message_count,
        is_active=session.is_active,
        messages=messages_out,
    )


@router.delete("/{session_id}", response_model=Dict[str, Any])
async def delete_session(session_id: str):
    """Delete a session and remove its persisted state."""
    sm = _session_manager()
    deleted = await sm.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"deleted": True, "session_id": session_id}
