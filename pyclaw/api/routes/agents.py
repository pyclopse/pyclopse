"""Agent API routes."""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("pyclaw.api.agents")

router = APIRouter()


# Request/Response models
class AgentConfigUpdate(BaseModel):
    """Agent configuration update."""
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None


class AgentResponse(BaseModel):
    """Agent information response."""
    id: str
    name: str
    model: str
    status: str
    session_count: int = 0


class SessionMessage(BaseModel):
    """A message in a session."""
    role: str  # system, user, assistant
    content: str


class SendMessageRequest(BaseModel):
    """Request to send a message to an agent."""
    content: str
    session_id: Optional[str] = None  # Create new session if not provided


class MessageResponse(BaseModel):
    """Response from sending a message."""
    session_id: str
    message_id: str
    content: str
    timestamp: str


class SessionResponse(BaseModel):
    """Session information."""
    id: str
    agent_id: str
    created_at: str
    message_count: int
    channel: Optional[str] = None


# Helper dependency
def get_gateway():
    """Get the gateway instance."""
    from pyclaw.api.app import get_gateway as _get_gateway
    return _get_gateway()


# List all agents
@router.get("/", response_model=Dict[str, Any])
async def list_agents():
    """List all available agents."""
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
    """Get information about a specific agent."""
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
    """Update agent configuration."""
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
    """Create a new session for an agent."""
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
            session_id = f"session-{datetime.now().timestamp()}"
            session = {"id": session_id, "agent_id": agent_id, "created_at": datetime.now()}
        
        return SessionResponse(
            id=session.get("id", "unknown"),
            agent_id=agent_id,
            created_at=session.get("created_at", datetime.now().isoformat()),
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
    """List all sessions for an agent."""
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


# Send message to agent
@router.post("/{agent_id}/messages", response_model=MessageResponse)
async def send_message(agent_id: str, request: SendMessageRequest):
    """Send a message to an agent (creates session if needed)."""
    try:
        gateway = get_gateway()
        
        if not hasattr(gateway, 'agents') or agent_id not in gateway.agents:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        
        agent = gateway.agents[agent_id]
        
        # Get or create session
        session_id = request.session_id
        if not session_id:
            if hasattr(agent, 'create_session'):
                session = await agent.create_session()
                session_id = session.get("id")
            else:
                session_id = f"session-{datetime.now().timestamp()}"
        
        # Send message
        if hasattr(agent, 'process_message'):
            result = await agent.process_message(session_id, request.content)
        else:
            result = {
                "message_id": f"msg-{datetime.now().timestamp()}",
                "content": "Message processed (stub)",
            }
        
        return MessageResponse(
            session_id=session_id,
            message_id=result.get("message_id", "unknown"),
            content=result.get("content", ""),
            timestamp=datetime.now().isoformat(),
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Get session messages
@router.get("/{agent_id}/sessions/{session_id}/messages", response_model=Dict[str, Any])
async def get_session_messages(agent_id: str, session_id: str):
    """Get messages from a session."""
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
