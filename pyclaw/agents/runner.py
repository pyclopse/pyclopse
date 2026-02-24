"""Agent runner for executing agent turns with FastAgent."""

import asyncio
import logging
from typing import Any, Dict, List, Optional, AsyncIterator

from .factory import get_factory, FASTAGENT_AVAILABLE


logger = logging.getLogger("pyclaw.agents.runner")


class AgentRunner:
    """Runner for executing agent turns using FastAgent.
    
    This class provides a simplified interface for running FastAgent
    agents within pyclaw's existing framework.
    """
    
    def __init__(
        self,
        agent_name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
    ):
        """Initialize the agent runner.
        
        Args:
            agent_name: Name of the agent
            instruction: System instruction
            model: Model to use
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            servers: MCP server names to attach
        """
        self.agent_name = agent_name
        self.instruction = instruction
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.servers = servers or []
        
        self._factory = get_factory()
        self._agent = None
        self._context: List[Dict[str, str]] = []
    
    async def initialize(self) -> None:
        """Initialize the FastAgent."""
        if not FASTAGENT_AVAILABLE:
            raise ImportError(
                "FastAgent is not installed. "
                "Install with: uv pip install fast-agent-mcp"
            )
        
        self._agent = self._factory.create_agent(
            name=self.agent_name,
            instruction=self.instruction,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            servers=self.servers,
        )
        
        logger.info(f"Initialized agent runner: {self.agent_name}")
    
    async def run(self, prompt: str) -> str:
        """Run a single prompt through the agent.
        
        Args:
            prompt: User prompt
            
        Returns:
            Agent response content
        """
        if self._agent is None:
            await self.initialize()
        
        async with self._agent.run() as agent:
            result = await agent(prompt)
            return str(result)
    
    async def run_stream(self, prompt: str) -> AsyncIterator[str]:
        """Run a prompt and stream the response.
        
        Args:
            prompt: User prompt
            
        Yields:
            Response chunks
        """
        if self._agent is None:
            await self.initialize()
        
        async with self._agent.run() as agent:
            async for chunk in agent.stream(prompt):
                yield chunk
    
    async def run_with_messages(
        self,
        messages: List[Dict[str, str]],
    ) -> str:
        """Run with a message history.
        
        Args:
            messages: List of messages with 'role' and 'content'
            
        Returns:
            Agent response
        """
        if self._agent is None:
            await self.initialize()
        
        # Build context from messages
        context = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                # Prepend to instruction
                self.instruction = f"{content}\n\n{self.instruction}"
            else:
                context.append({"role": role, "content": content})
        
        async with self._agent.run() as agent:
            # Send each message
            for msg in context:
                await agent.send(msg["content"], role=msg["role"])
            
            # Get final response
            result = await agent.get_response()
            return str(result) if result else ""
    
    def add_message(self, role: str, content: str) -> None:
        """Add a message to the context.
        
        Args:
            role: Message role (user/assistant/system)
            content: Message content
        """
        self._context.append({"role": role, "content": content})
    
    def clear_context(self) -> None:
        """Clear the message context."""
        self._context.clear()
    
    @property
    def context(self) -> List[Dict[str, str]]:
        """Get current message context."""
        return self._context.copy()


async def run_agent_turn(
    agent_name: str,
    prompt: str,
    instruction: str,
    model: str = "sonnet",
    **kwargs,
) -> str:
    """Convenience function to run a single agent turn.
    
    Args:
        agent_name: Name of the agent
        prompt: User prompt
        instruction: System instruction
        model: Model to use
        **kwargs: Additional arguments for AgentRunner
        
    Returns:
        Agent response
    """
    runner = AgentRunner(
        agent_name=agent_name,
        instruction=instruction,
        model=model,
        **kwargs,
    )
    
    return await runner.run(prompt)


async def run_workflow_turn(
    workflow_name: str,
    prompt: str,
    factory: Optional[Any] = None,
) -> str:
    """Run a workflow (chain/parallel/maker/etc.).
    
    Args:
        workflow_name: Name of the workflow
        prompt: User prompt
        factory: Optional FastAgentFactory instance
        
    Returns:
        Workflow response
    """
    if factory is None:
        factory = get_factory()
    
    workflow = factory.get_workflow(workflow_name)
    
    if workflow is None:
        raise ValueError(f"Workflow not found: {workflow_name}")
    
    async with factory._agents.get("__app__", factory).run() as agent:
        result = await getattr(agent, workflow_name)(prompt)
        return str(result)


__all__ = [
    "AgentRunner",
    "run_agent_turn",
    "run_workflow_turn",
]
