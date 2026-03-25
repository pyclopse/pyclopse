"""Agent API routes."""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from pyclawops.utils.time import now

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("pyclawops.api.agents")

router = APIRouter()


# Request/Response models
class AgentConfigUpdate(BaseModel):
    """Partial agent configuration update payload.

    All fields are optional; only the fields present in the request body
    are applied to the agent's current configuration.

    Attributes:
        model (Optional[str]): LLM model identifier to switch to.
        temperature (Optional[float]): Sampling temperature override.
        max_tokens (Optional[int]): Maximum output token limit override.
        system_prompt (Optional[str]): Replacement system prompt text.
    """
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None


class AgentResponse(BaseModel):
    """Agent information response model.

    Attributes:
        id (str): Unique agent identifier.
        name (str): Human-readable agent name.
        model (str): Currently configured LLM model.
        status (str): Operational status ("running" or "idle").
        session_count (int): Number of active sessions for this agent.
    """
    id: str
    name: str
    model: str
    status: str
    session_count: int = 0


class SessionMessage(BaseModel):
    """A single message within a session conversation.

    Attributes:
        role (str): Speaker role — one of "system", "user", or "assistant".
        content (str): Text content of the message.
    """
    role: str  # system, user, assistant
    content: str


class SendMessageRequest(BaseModel):
    """Request body for sending a message to an agent's active session.

    Attributes:
        content (str): The message text to send.
        session_id (Optional[str]): Specific session ID to target. When omitted
            the agent's current active session is used.
        channel (str): Channel name for the message (default "internal").
        sender (str): Display name of the sender (default "internal").
        sender_id (str): Unique identifier for the sender (default "internal").
    """
    content: str
    session_id: Optional[str] = None
    channel: str = "internal"
    sender: str = "internal"
    sender_id: str = "internal"


class MessageResponse(BaseModel):
    """Response returned after sending a message to an agent.

    Attributes:
        session_id (str): ID of the session the message was routed to.
        message_id (str): Unique identifier assigned to this message.
        content (str): Agent's reply text.
        timestamp (str): ISO-formatted timestamp of the response.
    """
    session_id: str
    message_id: str
    content: str
    timestamp: str


class SessionResponse(BaseModel):
    """Session information summary.

    Attributes:
        id (str): Unique session identifier.
        agent_id (str): ID of the agent that owns this session.
        created_at (str): ISO-formatted creation timestamp.
        message_count (int): Number of messages exchanged in this session.
        channel (Optional[str]): Channel the session is associated with.
    """
    id: str
    agent_id: str
    created_at: str
    message_count: int
    channel: Optional[str] = None


# Helper dependency
def get_gateway():
    """Retrieve the global gateway instance via the app module.

    Returns:
        Any: The gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway is not initialized.
    """
    from pyclawops.api.app import get_gateway as _get_gateway
    return _get_gateway()


# List all agents
@router.get("/", response_model=Dict[str, Any])
async def list_agents():
    """List all agents registered with the gateway.

    Returns:
        Dict[str, Any]: ``{"agents": [...]}`` where each entry contains
            the agent's id, name, model, and status.

    Raises:
        HTTPException: With status 500 on unexpected errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents'):
            return {"agents": []}
        
        agents = []
        for agent_id, agent in gateway.agents.items():
            agents.append({
                "id": agent_id,
                "name": agent.name if hasattr(agent, 'name') else agent_id,
                "model": agent.model if hasattr(agent, 'model') else "unknown",
                "status": "running" if hasattr(agent, 'is_running') and agent.is_running else "idle",
            })
        
        return {"agents": agents}
    
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Get agent info
@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str):
    """Return detailed information about a specific agent.

    Args:
        agent_id (str): Unique identifier of the agent.

    Returns:
        AgentResponse: Agent details including model and status.

    Raises:
        HTTPException: 404 if the agent is not found; 500 on unexpected errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        return AgentResponse(
            id=agent_id,
            name=agent.name if hasattr(agent, 'name') else agent_id,
            model=agent.model if hasattr(agent, 'model') else "unknown",
            status="running" if hasattr(agent, 'is_running') and agent.is_running else "idle",
            session_count=len(agent.sessions) if hasattr(agent, 'sessions') else 0,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Update agent config
@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, config: AgentConfigUpdate):
    """Partially update an agent's runtime configuration.

    Only fields present in the request body are applied; all others retain
    their current values.

    Args:
        agent_id (str): Unique identifier of the agent.
        config (AgentConfigUpdate): Fields to update.

    Returns:
        AgentResponse: Updated agent details.

    Raises:
        HTTPException: 404 if the agent is not found; 500 on unexpected errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        # Apply config updates
        if config.model and hasattr(agent, 'model'):
            agent.model = config.model
        if config.temperature and hasattr(agent, 'temperature'):
            agent.temperature = config.temperature
        if config.max_tokens and hasattr(agent, 'max_tokens'):
            agent.max_tokens = config.max_tokens
        if config.system_prompt and hasattr(agent, 'system_prompt'):
            agent.system_prompt = config.system_prompt
        
        return AgentResponse(
            id=agent_id,
            name=agent.name if hasattr(agent, 'name') else agent_id,
            model=agent.model if hasattr(agent, 'model') else "unknown",
            status="running" if hasattr(agent, 'is_running') and agent.is_running else "idle",
            session_count=len(agent.sessions) if hasattr(agent, 'sessions') else 0,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Create/get session
@router.post("/{agent_id}/sessions", response_model=SessionResponse)
async def create_session(agent_id: str, channel: Optional[str] = None):
    """Create a new conversation session for the specified agent.

    Args:
        agent_id (str): Unique identifier of the agent.
        channel (Optional[str]): Channel name to associate with the session.

    Returns:
        SessionResponse: Metadata about the newly created session.

    Raises:
        HTTPException: 404 if the agent is not found; 500 on unexpected errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        # Create new session
        if hasattr(agent, 'create_session'):
            session = await agent.create_session(channel=channel)
        else:
            # Simple session creation
            session_id = f"session-{now().timestamp()}"
            session = {"id": session_id, "agent_id": agent_id, "created_at": now()}
        
        return SessionResponse(
            id=session.get("id", "unknown"),
            agent_id=agent_id,
            created_at=session.get("created_at", now().isoformat()),
            message_count=0,
            channel=channel,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# List sessions for agent
@router.get("/{agent_id}/sessions", response_model=Dict[str, Any])
async def list_sessions(agent_id: str):
    """Return all sessions associated with the given agent.

    Args:
        agent_id (str): Unique identifier of the agent.

    Returns:
        Dict[str, Any]: ``{"sessions": [...]}`` with each session's id,
            created_at, and message_count.

    Raises:
        HTTPException: 404 if the agent is not found; 500 on unexpected errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        sessions = []
        if hasattr(agent, 'sessions'):
            for session_id, session in agent.sessions.items():
                sessions.append({
                    "id": session_id,
                    "created_at": session.get("created_at", "unknown"),
                    "message_count": session.get("message_count", 0),
                })
        
        return {"sessions": sessions}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Send message to agent's active session
@router.post("/{agent_id}/messages")
async def send_message(agent_id: str, request: SendMessageRequest):
    """Send a message into an agent's active session and return the response.

    Routes the message through the gateway's standard message pipeline,
    which uses the agent's single active session (shared across all channels).
    This means the agent answers with full conversation context intact.

    Use ``session_id`` to target a specific session; omit to use the active one.
    """
    import uuid

    gateway = get_gateway()
    am = getattr(gateway, "_agent_manager", None)
    if not am or agent_id not in (getattr(am, "agents", {}) or {}):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    message_id = str(uuid.uuid4())

    try:
        response = await gateway.handle_message(
            channel=request.channel,
            sender=request.sender,
            sender_id=request.sender_id,
            content=request.content,
            agent_id=agent_id,
            message_id=message_id,
        )
    except Exception as e:
        logger.error(f"send_message error for agent '{agent_id}': {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Resolve the session that was used
    sm = getattr(gateway, "_session_manager", None)
    session_id = request.session_id
    if not session_id and sm:
        try:
            active = await sm.get_active_session(agent_id)
            if active:
                session_id = active.id
        except Exception:
            pass

    return {
        "response": response or "",
        "session_id": session_id or "unknown",
        "agent_id": agent_id,
        "message_id": message_id,
    }


# Get session messages
@router.get("/{agent_id}/sessions/{session_id}/messages", response_model=Dict[str, Any])
async def get_session_messages(agent_id: str, session_id: str):
    """Retrieve all messages for a specific agent session.

    Args:
        agent_id (str): Unique identifier of the agent.
        session_id (str): Unique identifier of the session.

    Returns:
        Dict[str, Any]: ``{"session_id": ..., "messages": [...]}`` with each
            message's role and content.

    Raises:
        HTTPException: 404 if agent or session is not found; 500 on errors.
    """
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        if not hasattr(agent, 'sessions') or session_id not in agent.sessions:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        
        session = agent.sessions[session_id]
        messages = session.get("messages", [])
        
        return {"session_id": session_id, "messages": messages}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))
