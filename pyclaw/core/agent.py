"""Agent management for pyclaw with FastAgent integration."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pyclaw.config.schema import AgentConfig as ConfigModel
from pyclaw.config.schema import AgentConfig
from pyclaw.core.session import Session, Message as SessionMessage
from pyclaw.core.router import IncomingMessage, OutgoingMessage
from pyclaw.providers import (
    Message as ProviderMessage,
    ChatResponse,
    create_provider,
    get_registry as get_provider_registry,
)

# FastAgent imports
try:
    from fast_agent import FastAgent
    FASTAGENT_AVAILABLE = True
except ImportError:
    FastAgent = None
    FASTAGENT_AVAILABLE = False

from pyclaw.agents.factory import (
    FastAgentFactory,
    create_agent_from_config,
    get_factory,
)
from pyclaw.core.prompt_builder import build_system_prompt, AGENT_FILES


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
    skill_runner: Optional[Any] = None  # SkillRunner for tool execution
    config_dir: str = "~/.pyclaw"  # Base config directory for agent files
    
    # FastAgent integration
    fast_agent: Optional[Any] = None  # FastAgent instance
    fast_agent_runner: Optional[Any] = None  # AgentRunner instance
    
    # Runtime state
    is_running: bool = False
    current_session: Optional[Session] = None
    _tasks: List[asyncio.Task] = field(default_factory=list)
    _logger: logging.Logger = field(init=False)
    
    def __post_init__(self):
        object.__setattr__(self, '_logger', logging.getLogger(f"pyclaw.agent.{self.id}"))
        
        # Initialize FastAgent if available and configured
        if FASTAGENT_AVAILABLE and self._should_use_fastagent():
            self._init_fastagent()
    
    def _should_use_fastagent(self) -> bool:
        """Check if agent should use FastAgent."""
        model = self.config.model.lower()
        return (
            model.startswith("fastagent") or
            model.startswith("fa:") or
            getattr(self.config, "use_fastagent", False) or
            getattr(self.config, "workflow", None) is not None
        )
    
    @property
    def system_prompt(self) -> str:
        """Get system prompt - built from agent files or config."""
        # Try to build from agent files (like OpenClaw)
        if hasattr(self, 'config_dir'):
            prompt = build_system_prompt(self.id, self.config_dir)
            if prompt != "You are a helpful AI assistant.":
                return prompt
        
        # Fall back to config
        return getattr(self.config, "system_prompt", "You are a helpful AI assistant.")
    
    def _init_fastagent(self) -> None:
        """Initialize FastAgent for this agent."""
        if not FASTAGENT_AVAILABLE:
            self._logger.warning("FastAgent not available")
            return
        
        try:
            factory = get_factory()
            
            # Get workflow type from config
            workflow_type = getattr(self.config, "workflow", None)
            
            if workflow_type:
                # Create workflow agent
                workflow_config = {
                    "name": self.name,
                    "instruction": self.system_prompt,
                    "workflow": workflow_type,
                    "agents": getattr(self.config, "agents", []),
                    "model": self.config.model,
                    "temperature": self.config.temperature,
                    "servers": getattr(self.config, "mcp_servers", []),
                }
                self.fast_agent = create_agent_from_config(workflow_config)
            else:
                # Create regular FastAgent
                model = self.config.model
                for prefix in ["fastagent:", "fa:", "fastagent/"]:
                    model = model.replace(prefix, "")
                
                self.fast_agent = factory.create_agent(
                    name=self.name,
                    instruction=self.system_prompt,
                    model=model or "sonnet",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    servers=getattr(self.config, "mcp_servers", []),
                )
            
            # Create runner for turn-based execution
            from pyclaw.agents.runner import AgentRunner
            # Strip prefix from model for runner
            runner_model = self.config.model
            for prefix in ["fastagent:", "fa:", "fastagent/"]:
                runner_model = runner_model.replace(prefix, "")
            self.fast_agent_runner = AgentRunner(
                agent_name=self.name,
                instruction=self.system_prompt,
                model=runner_model or "sonnet",
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                servers=getattr(self.config, "mcp_servers", []),
            )
            
            self._logger.info(f"Initialized FastAgent for {self.name}")
            
        except Exception as e:
            self._logger.error(f"Failed to initialize FastAgent: {e}")
    
    async def start(self) -> None:
        """Start the agent."""
        self.is_running = True
        self._logger.info(f"Agent {self.name} started")
        
        # Initialize FastAgent runner if available
        if self.fast_agent_runner:
            await self.fast_agent_runner.initialize()
    
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
            # Use FastAgent if available
            if self.fast_agent_runner:
                response_content = await self._handle_with_fastagent(message.content, session)
            else:
                # Fall back to provider-based handling
                messages = self._build_messages(session)
                tools = None
                if self.config.tools.enabled and self.skill_runner:
                    tools = self.skill_runner.registry.get_tools()
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
    
    async def _handle_with_fastagent(
        self,
        prompt: str,
        session: Session,
    ) -> str:
        """Handle message using FastAgent."""
        # Build message history for context
        messages = []
        for msg in session.get_context_window(max_messages=20):
            messages.append({
                "role": msg.role,
                "content": msg.content,
            })
        
        # Add current prompt
        messages.append({"role": "user", "content": prompt})
        
        # Run with FastAgent
        return await self.fast_agent_runner.run(messages)
    
    def _build_messages(self, session: Session) -> List[ProviderMessage]:
        """Build message list for provider."""
        messages = []
        
        # Add system prompt
        messages.append(ProviderMessage(
            role="system",
            content=self.system_prompt,
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
            
            # Tool execution requires FastAgent
            # Use FastAgent for agents that need tool execution
            for tool_call in response.tool_calls:
                messages.append(ProviderMessage(
                    role="tool",
                    content="Tool execution requires FastAgent. Use a FastAgent-based agent for tools.",
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
        
        # Use FastAgent if available
        if self.fast_agent_runner:
            try:
                return await self.fast_agent_runner.run(prompt)
            except Exception as e:
                self._logger.error(f"FastAgent heartbeat error: {e}")
        
        # Fall back to provider
        messages = [
            ProviderMessage(role="system", content=self.system_prompt),
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
            "fast_agent": self.fast_agent is not None,
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
            print(f"DEBUG: provider_config = {provider_config}")
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
