"""Agent management for pyclaw."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

from pyclaw.config.schema import AgentConfig as ConfigModel
from pyclaw.core.session import Session, Message
from pyclaw.core.router import IncomingMessage, OutgoingMessage


# Tool execution function type
ToolExecutor = Callable[[str, List[str], str], Awaitable[Dict[str, Any]]]


@dataclass
class Agent:
    """Agent that handles conversations."""
    id: str
    name: str
    config: ConfigModel
    session_manager: Any = None  # SessionManager
    tool_executor: Optional[ToolExecutor] = None
    provider: Optional[Any] = None  # Provider
    
    # Runtime state
    is_running: bool = False
    current_session: Optional[Session] = None
    _tasks: List[asyncio.Task] = field(default_factory=list)
    _logger: logging.Logger = field(init=False)
    
    def __post_init__(self):
        object.__setattr__(self, '_logger', logging.getLogger(f"pyclaw.agent.{self.id}"))
    
    async def start(self) -> None:
        """Start the agent."""
        self.is_running = True
        self._logger.info(f"Agent {self.name} started")
    
    async def stop(self) -> None:
        """Stop the agent."""
        self.is_running = False
        
        # Cancel pending tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        self._tasks.clear()
        self._logger.info(f"Agent {self.name} stopped")
    
    async def handle_message(
        self,
        message: IncomingMessage,
        session: Session,
    ) -> Optional[OutgoingMessage]:
        """Handle an incoming message."""
        self.current_session = session
        
        # Add user message to session
        session.add_message(
            role="user",
            content=message.content,
            metadata={
                "message_id": message.id,
                "channel": message.channel,
                "sender": message.sender,
            },
        )
        
        try:
            # Build messages for provider
            messages = self._build_messages(session)
            
            # Get response from provider
            response_content = await self._get_response(messages)
            
            # Add assistant response to session
            session.add_message(
                role="assistant",
                content=response_content,
            )
            
            return OutgoingMessage(
                content=response_content,
                target=message.sender_id,
                channel=message.channel,
                reply_to=message.id,
            )
            
        except Exception as e:
            self._logger.error(f"Error handling message: {e}")
            session.add_message(
                role="system",
                content=f"Error: {str(e)}",
            )
            return OutgoingMessage(
                content=f"I encountered an error: {str(e)}",
                target=message.sender_id,
                channel=message.channel,
            )
    
    def _build_messages(self, session: Session) -> List[Dict[str, Any]]:
        """Build message list for provider."""
        messages = []
        
        # Add system prompt
        messages.append({
            "role": "system",
            "content": self.config.system_prompt,
        })
        
        # Add conversation history
        for msg in session.get_context_window(max_messages=20):
            messages.append({
                "role": msg.role,
                "content": msg.content,
            })
        
        return messages
    
    async def _get_response(
        self,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Get response from provider."""
        if self.provider is None:
            return "No provider configured"
        
        # This would call the actual provider
        # For now, return a placeholder
        return "Response from provider"
    
    async def execute_tool(
        self,
        tool_name: str,
        args: List[str],
        cwd: str,
    ) -> Dict[str, Any]:
        """Execute a tool."""
        if self.tool_executor is None:
            return {
                "success": False,
                "error": "No tool executor configured",
            }
        
        return await self.tool_executor(tool_name, args, cwd)
    
    async def run_heartbeat(
        self,
        prompt: str,
    ) -> Optional[str]:
        """Run a heartbeat check."""
        if not self.config.heartbeat.enabled:
            return None
        
        # Check active hours
        active_hours = self.config.heartbeat.active_hours
        if active_hours:
            now = datetime.now()
            start = datetime.strptime(active_hours.get("start", "00:00"), "%H:%M")
            end = datetime.strptime(active_hours.get("end", "23:59"), "%H:%M")
            
            if not (start.time() <= now.time() <= end.time()):
                return None
        
        # Run heartbeat
        self._logger.debug("Running heartbeat")
        
        # Build heartbeat message
        messages = [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": prompt},
        ]
        
        try:
            response = await self._get_response(messages)
            return response
        except Exception as e:
            self._logger.error(f"Heartbeat error: {e}")
            return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get agent status."""
        return {
            "id": self.id,
            "name": self.name,
            "is_running": self.is_running,
            "model": self.config.model,
            "session_id": self.current_session.id if self.current_session else None,
            "pending_tasks": len(self._tasks),
        }
    
    def update_config(self, **updates) -> None:
        """Update agent configuration."""
        for key, value in updates.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        self._logger.info(f"Updated config: {updates}")


class AgentManager:
    """Manages multiple agents."""
    
    def __init__(self):
        self.agents: Dict[str, Agent] = {}
        self._default_agent_id: Optional[str] = None
        self._logger = logging.getLogger("pyclaw.agent_manager")
    
    def create_agent(
        self,
        agent_id: str,
        name: str,
        config: ConfigModel,
        **kwargs,
    ) -> Agent:
        """Create a new agent."""
        if agent_id in self.agents:
            raise ValueError(f"Agent {agent_id} already exists")
        
        agent = Agent(
            id=agent_id,
            name=name,
            config=config,
            **kwargs,
        )
        
        self.agents[agent_id] = agent
        
        if self._default_agent_id is None:
            self._default_agent_id = agent_id
        
        self._logger.info(f"Created agent: {name} ({agent_id})")
        
        return agent
    
    async def start_agent(self, agent_id: str) -> bool:
        """Start an agent."""
        agent = self.agents.get(agent_id)
        if agent:
            await agent.start()
            return True
        return False
    
    async def stop_agent(self, agent_id: str) -> bool:
        """Stop an agent."""
        agent = self.agents.get(agent_id)
        if agent:
            await agent.stop()
            return True
        return False
    
    async def start_all(self) -> None:
        """Start all agents."""
        for agent in self.agents.values():
            await agent.start()
    
    async def stop_all(self) -> None:
        """Stop all agents."""
        for agent in self.agents.values():
            await agent.stop()
    
    def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Get an agent by ID."""
        return self.agents.get(agent_id)
    
    def get_default_agent(self) -> Optional[Agent]:
        """Get the default agent."""
        if self._default_agent_id:
            return self.agents.get(self._default_agent_id)
        return None
    
    def set_default_agent(self, agent_id: str) -> bool:
        """Set the default agent."""
        if agent_id in self.agents:
            self._default_agent_id = agent_id
            return True
        return False
    
    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent."""
        agent = self.agents.pop(agent_id, None)
        if agent:
            if self._default_agent_id == agent_id:
                self._default_agent_id = (
                    list(self.agents.keys())[0] if self.agents else None
                )
            self._logger.info(f"Removed agent: {agent_id}")
            return True
        return False
    
    def list_agents(self) -> List[Agent]:
        """List all agents."""
        return list(self.agents.values())
    
    def get_status(self) -> Dict[str, Any]:
        """Get agent manager status."""
        return {
            "total_agents": len(self.agents),
            "running_agents": len([a for a in self.agents.values() if a.is_running]),
            "default_agent": self._default_agent_id,
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "is_running": a.is_running,
                    "model": a.config.model,
                }
                for a in self.agents.values()
            ],
        }
