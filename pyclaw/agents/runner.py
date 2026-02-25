"""Agent runner for executing agent turns with FastAgent.

This module provides a simplified interface for running FastAgent
agents within pyclaw's existing framework.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, AsyncIterator, Union

from .factory import get_factory, FASTAGENT_AVAILABLE, FastAgentFactory


logger = logging.getLogger("pyclaw.agents.runner")


class AgentRunner:
    """Runner for executing agent turns using FastAgent.
    
    This class provides a simplified interface for running FastAgent
    agents within pyclaw's existing framework.
    
    Example:
        runner = AgentRunner("my_agent", "You are a helpful assistant.")
        result = await runner.run("Hello!")
    """
    
    def __init__(
        self,
        agent_name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
        config_path: Optional[str] = None,
    ):
        """Initialize the agent runner.
        
        Args:
            agent_name: Name of the agent
            instruction: System instruction
            model: Model to use (sonnet, haiku, opus, etc.)
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            servers: MCP server names to attach
            config_path: Optional path to YAML config
        """
        self.agent_name = agent_name
        self.instruction = instruction
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.servers = servers or []
        self.config_path = config_path
        
        self._factory: Optional[FastAgentFactory] = None
        self._context: Optional[Any] = None
        self._message_history: List[Dict[str, str]] = []
    
    def _get_factory(self) -> FastAgentFactory:
        """Get or create the factory."""
        if self._factory is None:
            if self.config_path:
                self._factory = FastAgentFactory(self.config_path)
            else:
                self._factory = get_factory()
        return self._factory
    
    async def initialize(self) -> None:
        """Initialize the agent runner."""
        if not FASTAGENT_AVAILABLE:
            raise ImportError(
                "FastAgent is not available. "
                "Install with: uv pip install fast-agent-mcp"
            )
        
        from fast_agent import Context
        
        self._context = Context(
            name=self.agent_name,
            instruction=self.instruction,
            model=self.model,
            # Note: temperature/max_tokens handled via request_params if needed
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
        if self._context is None:
            await self.initialize()
        
        # Add to history
        self._message_history.append({"role": "user", "content": prompt})
        
        # Run without context manager (not needed)
        result = await self._context.send(prompt)
        response = str(result)
        
        # Add response to history
        self._message_history.append({"role": "assistant", "content": response})
        
        return response
    
    async def run_stream(self, prompt: str) -> AsyncIterator[str]:
        """Run a prompt and stream the response.
        
        Args:
            prompt: User prompt
            
        Yields:
            Response chunks
        """
        if self._context is None:
            await self.initialize()
        
        self._message_history.append({"role": "user", "content": prompt})
        
        # Stream without context manager
        async for chunk in self._context.stream(prompt):
            yield str(chunk)
    
    async def run_with_history(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> str:
        """Run with explicit message history.
        
        Args:
            messages: List of messages with 'role' and 'content'
            system_prompt: Optional system prompt to prepend
            
        Returns:
            Agent response
        """
        if self._context is None:
            await self.initialize()
        
        if system_prompt:
            self.instruction = f"{system_prompt}\n\n{self.instruction}"
        
        
            # Send each message in sequence
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                await self._context.send(content, role=role)
            
            # Get final response
            result = await self._context.get_final()
            response = str(result) if result else ""
            
            self._message_history.extend(messages)
            self._message_history.append({"role": "assistant", "content": response})
            
            return response
    
    def add_message(self, role: str, content: str) -> None:
        """Add a message to the context history.
        
        Args:
            role: Message role (user/assistant/system)
            content: Message content
        """
        self._message_history.append({"role": role, "content": content})
    
    def clear_history(self) -> None:
        """Clear the message history."""
        self._message_history.clear()
    
    @property
    def history(self) -> List[Dict[str, str]]:
        """Get current message history."""
        return self._message_history.copy()
    
    @property
    def context(self) -> Optional[Any]:
        """Get the underlying FastAgent Context."""
        return self._context


class WorkflowRunner:
    """Runner for executing FastAgent workflows (chains, parallel, etc).
    
    Note: Full workflow support requires generating Python code with
    decorators. This runner provides a simplified interface for common cases.
    """
    
    def __init__(self, config_path: str = "agents.yaml"):
        """Initialize workflow runner.
        
        Args:
            config_path: Path to YAML config with workflow definitions
        """
        self.config_path = config_path
        self._factory = FastAgentFactory(config_path)
        self._config: Dict[str, Any] = {}
    
    def load_config(self) -> Dict[str, Any]:
        """Load the workflow configuration."""
        self._config = self._factory.load_config()
        return self._config
    
    def list_workflows(self) -> List[str]:
        """List available workflows."""
        if not self._config:
            self.load_config()
        return list(self._config.get('workflows', {}).keys())
    
    def list_agents(self) -> List[str]:
        """List available agents."""
        if not self._config:
            self.load_config()
        return list(self._config.get('agents', {}).keys())
    
    def get_workflow_spec(self, name: str) -> Optional[Dict[str, Any]]:
        """Get workflow specification."""
        if not self._config:
            self.load_config()
        return self._config.get('workflows', {}).get(name)
    
    def generate_code(self, output_path: Optional[str] = None) -> str:
        """Generate FastAgent Python code from config.
        
        Args:
            output_path: Optional path to write code
            
        Returns:
            Generated Python code
        """
        return self._factory.compile_to_code()
    
    def run_workflow(
        self,
        workflow_name: str,
        message: str,
    ) -> str:
        """Run a workflow.
        
        Note: For full workflow support (chains, parallel, etc),
        generate the code and run it directly. This is a convenience
        method that works for simple agent execution.
        
        Args:
            workflow_name: Name of workflow to run
            message: Message to send
            
        Returns:
            Workflow response
        """
        # For now, fall back to running the default/first agent
        # Full workflow execution requires generated code
        agents = self.list_agents()
        if not agents:
            raise ValueError("No agents defined in config")
        
        # Use the first agent as fallback
        agent_name = agents[0]
        factory = get_factory(self.config_path)
        return factory.run_agent(agent_name, message)


# Convenience functions

async def run_agent(
    agent_name: str,
    prompt: str,
    instruction: str = "You are a helpful assistant.",
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


async def run_workflow(
    workflow_name: str,
    prompt: str,
    config_path: str = "agents.yaml",
) -> str:
    """Run a workflow from config.
    
    Args:
        workflow_name: Name of the workflow
        prompt: User prompt
        config_path: Path to YAML config
        
    Returns:
        Workflow response
    """
    runner = WorkflowRunner(config_path)
    return runner.run_workflow(workflow_name, prompt)


# Alias for backwards compatibility
run_agent_turn = run_agent


__all__ = [
    "AgentRunner",
    "WorkflowRunner", 
    "run_agent",
    "run_agent_turn",
    "run_workflow",
]
