"""Agent management for pyclaw."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pyclaw.config.schema import AgentConfig as ConfigModel
from pyclaw.core.session import Session, Message as SessionMessage
from pyclaw.core.router import IncomingMessage, OutgoingMessage
from pyclaw.providers import (
    Message as ProviderMessage,
    ChatResponse,
    create_provider,
    get_registry as get_provider_registry,
)
from pyclaw.skills import (
    get_registry as get_skill_registry,
    SkillRunner,
    SkillContext,
)
from pyclaw.skills.runner import get_default_runner


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
    provider: Optional[Any] = None  # Provider instance
    skill_runner: Optional[SkillRunner] = None
    
    # Runtime state
    is_running: bool = False
    current_session: Optional[Session] = None
    _tasks: List[asyncio.Task] = field(default_factory=list)
    _logger: logging.Logger = field(init=False)
    
    def __post_init__(self):
        object.__setattr__(self, '_logger', logging.getLogger(f"pyclaw.agent.{self.id}"))
        
        # Initialize skill runner if tools are enabled
        if self.config.tools.enabled and self.skill_runner is None:
            self.skill_runner = get_default_runner()
    
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
            
            # Get available tools if enabled
            tools = None
            if self.config.tools.enabled and self.skill_runner:
                tools = self.skill_runner.registry.get_tools()
            
            # Get response from provider (with tool execution loop)
            response_content = await self._get_response_with_tools(messages, tools)
            
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
    
    def _build_messages(self, session: Session) -> List[ProviderMessage]:
        """Build message list for provider."""
        messages = []
        
        # Add system prompt
        messages.append(ProviderMessage(
            role="system",
            content=self.config.system_prompt,
        ))
        
        # Add conversation history
        for msg in session.get_context_window(max_messages=20):
            messages.append(ProviderMessage(
                role=msg.role,
                content=msg.content,
            ))
        
        return messages
    
    async def _get_response_with_tools(
        self,
        messages: List[ProviderMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Get response from provider with tool execution loop."""
        if self.provider is None:
            return "No provider configured"
        
        max_tool_iterations = 5  # Prevent infinite loops
        iteration = 0
        
        while iteration < max_tool_iterations:
            iteration += 1
            
            # Get response from provider
            response: ChatResponse = await self.provider.chat(
                messages=messages,
                model=self.config.model,
                tools=tools,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            
            # Add assistant message to conversation
            messages.append(ProviderMessage(
                role="assistant",
                content=response.content,
                tool_calls=[
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in (response.tool_calls or [])
                ] if response.tool_calls else None,
            ))
            
            # If no tool calls, return the content
            if not response.tool_calls:
                return response.content
            
            # Execute tool calls
            for tool_call in response.tool_calls:
                if not self.skill_runner:
                    # No skill runner, add error message
                    messages.append(ProviderMessage(
                        role="tool",
                        content="Skill runner not configured",
                        tool_call_id=tool_call.id,
                        name=tool_call.name,
                    ))
                    continue
                
                # Create context for skill execution
                context = SkillContext(
                    agent_id=self.id,
                    session_id=self.current_session.id if self.current_session else "unknown",
                )
                
                # Execute the tool
                tool_result = await self.skill_runner.execute_tool_call(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    context=context,
                )
                
                # Add tool result to conversation
                messages.append(ProviderMessage(
                    role="tool",
                    content=tool_result.get("error") or str(tool_result.get("output", "")),
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                ))
        
        # Max iterations reached
        return "Maximum tool execution iterations reached"
    
    async def _get_response(
        self,
        messages: List[ProviderMessage],
    ) -> str:
        """Get response from provider (simple version without tools)."""
        if self.provider is None:
            return "No provider configured"
        
        response: ChatResponse = await self.provider.chat(
            messages=messages,
            model=self.config.model,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        
        return response.content
    
    async def execute_tool(
        self,
        tool_name: str,
        args: List[str],
        cwd: str,
    ) -> Dict[str, Any]:
        """Execute a tool."""
        if self.skill_runner:
            # Use skill runner
            result = await self.skill_runner.execute(
                skill_name=tool_name,
                args={"args": args, "cwd": cwd},
            )
            return {
                "success": result.success,
                "result": result.result,
                "error": result.error,
            }
        
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
            ProviderMessage(role="system", content=self.config.system_prompt),
            ProviderMessage(role="user", content=prompt),
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
            "provider": type(self.provider).__name__ if self.provider else None,
            "skills": len(self.skill_runner.registry.list_skills()) if self.skill_runner else 0,
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
        provider_config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Agent:
        """Create a new agent with optional provider."""
        if agent_id in self.agents:
            raise ValueError(f"Agent {agent_id} already exists")
        
        # Create provider if config provided
        provider = None
        if provider_config:
            provider_type = provider_config.get("type", "openai")
            provider = create_provider(provider_type, provider_config)
        
        agent = Agent(
            id=agent_id,
            name=name,
            config=config,
            provider=provider,
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


# Type alias for compatibility
Message = SessionMessage
