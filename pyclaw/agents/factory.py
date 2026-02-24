"""FastAgent factory for creating agents from configuration."""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Callable, Awaitable

# Try to import FastAgent - it's an optional dependency
try:
    from fast_agent import FastAgent
    FASTAGENT_AVAILABLE = True
except ImportError:
    FastAgent = None
    FASTAGENT_AVAILABLE = False


logger = logging.getLogger("pyclaw.agents.factory")


class FastAgentFactory:
    """Factory for creating FastAgent instances from configuration.
    
    This factory creates FastAgent-based agents for pyclaw, supporting
    all workflow patterns (chain, parallel, maker, agents-as-tools).
    """
    
    def __init__(self, config_path: str = "fastagent.config.yaml"):
        """Initialize the factory.
        
        Args:
            config_path: Path to FastAgent config YAML file.
        """
        self.config_path = config_path
        self._agents: Dict[str, Any] = {}
        self._workflows: Dict[str, Any] = {}
    
    @property
    def is_available(self) -> bool:
        """Check if FastAgent is available."""
        return FASTAGENT_AVAILABLE
    
    def _agentensure_fast(self) -> None:
        """Ensure FastAgent is available, raise error if not."""
        if not FASTAGENT_AVAILABLE:
            raise ImportError(
                "FastAgent is not installed. "
                "Install with: uv pip install fast-agent-mcp"
            )
    
    def create_agent(
        self,
        name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
        human_input: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> "FastAgent":
        """Create a FastAgent from configuration.
        
        Args:
            name: Agent name
            instruction: System instruction/prompt
            model: Model to use (default: sonnet)
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            servers: List of MCP server names to attach
            human_input: Enable human-in-the-loop
            tools: Optional tool definitions
            
        Returns:
            Configured FastAgent instance
        """
        self._ensure_fastagent()
        
        fast = FastAgent(name)
        
        @fast.agent(
            name=name,
            instruction=instruction,
            human_input=human_input,
            servers=servers or [],
        )
        async def agent_main():
            async with fast.run() as agent:
                await agent.interactive()
        
        # Store reference
        self._agents[name] = fast
        
        logger.info(f"Created FastAgent: {name}")
        
        return fast
    
    def create_chain_workflow(
        self,
        name: str,
        sequence: List[str],
        instruction: Optional[str] = None,
    ) -> Any:
        """Create a chain workflow (sequential execution).
        
        Args:
            name: Workflow name
            sequence: List of agent names to chain
            instruction: Optional instruction for the chain
            
        Returns:
            Chain workflow decorator
        """
        self._ensure_fastagent()
        
        # Create agents if they don't exist
        for agent_name in sequence:
            if agent_name not in self._agents:
                logger.warning(f"Agent {agent_name} not found, creating default")
                self.create_agent(agent_name, f"You are {agent_name}.")
        
        fast = FastAgent(name)
        
        # Apply decorators in reverse order (bottom-up)
        for agent_name in reversed(sequence):
            @fast.agent(
                name=agent_name,
                instruction=f"Agent {agent_name}",
            )
            pass
        
        chain = fast.chain(
            name=name,
            sequence=sequence,
        )
        
        self._workflows[name] = chain
        logger.info(f"Created chain workflow: {name} -> {sequence}")
        
        return chain
    
    def create_parallel_workflow(
        self,
        name: str,
        fan_out: List[str],
        fan_in: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> Any:
        """Create a parallel workflow (fan-out/fan-in).
        
        Args:
            name: Workflow name
            fan_out: List of agent names to execute in parallel
            fan_in: Optional agent to aggregate results
            instruction: Optional instruction for the workflow
            
        Returns:
            Parallel workflow decorator
        """
        self._ensure_fastagent()
        
        fast = FastAgent(name)
        
        # Create fan-out agents
        for agent_name in fan_out:
            @fast.agent(
                name=agent_name,
                instruction=f"Agent {agent_name}",
            )
            pass
        
        # Create fan-in agent if specified
        if fan_in:
            @fast.agent(
                name=fan_in,
                instruction=f"Agent {fan_in}",
            )
            pass
        
        parallel = fast.parallel(
            name=name,
            fan_out=fan_out,
            fan_in=fan_in,
        )
        
        self._workflows[name] = parallel
        logger.info(f"Created parallel workflow: {name} -> {fan_out}")
        
        return parallel
    
    def create_maker_workflow(
        self,
        name: str,
        worker: str,
        k: int = 3,
        max_samples: int = 25,
        match_strategy: str = "normalized",
        instruction: Optional[str] = None,
    ) -> Any:
        """Create a maker workflow (k-voting error reduction).
        
        Args:
            name: Workflow name
            worker: Name of the worker agent to sample
            k: Number of votes required
            max_samples: Maximum samples before giving up
            match_strategy: Strategy for matching responses
            instruction: Optional instruction
            
        Returns:
            Maker workflow decorator
        """
        self._ensure_fastagent()
        
        fast = FastAgent(name)
        
        # Create worker agent
        @fast.agent(
            name=worker,
            instruction=instruction or f"You are {worker}.",
        )
        pass
        
        maker = fast.maker(
            name=name,
            worker=worker,
            k=k,
            max_samples=max_samples,
            match_strategy=match_strategy,
        )
        
        self._workflows[name] = maker
        logger.info(f"Created maker workflow: {name} (k={k})")
        
        return maker
    
    def create_agents_as_tools_workflow(
        self,
        name: str,
        agents: List[str],
        instruction: str,
        default: bool = True,
        servers: Optional[List[str]] = None,
    ) -> Any:
        """Create an agents-as-tools workflow.
        
        This pattern exposes child agents as tools to an orchestrator,
        enabling routing, parallelization, and decomposition.
        
        Args:
            name: Orchestrator agent name
            agents: List of child agent names to expose as tools
            instruction: Orchestrator instruction
            default: Whether this is the default agent
            servers: MCP servers to attach
            
        Returns:
            Agents-as-tools workflow decorator
        """
        self._ensure_fastagent()
        
        fast = FastAgent(name)
        
        # Create child agents
        for agent_name in agents:
            @fast.agent(
                name=agent_name,
                instruction=f"You are {agent_name}.",
                servers=servers or [],
            )
            pass
        
        # Create orchestrator that uses agents as tools
        @fast.agent(
            name=name,
            instruction=instruction,
            default=default,
            agents=agents,
        )
        pass
        
        logger.info(f"Created agents-as-tools workflow: {name} -> {agents}")
        
        return fast
    
    def get_agent(self, name: str) -> Optional[Any]:
        """Get a registered agent by name."""
        return self._agents.get(name)
    
    def get_workflow(self, name: str) -> Optional[Any]:
        """Get a registered workflow by name."""
        return self._workflows.get(name)
    
    def list_agents(self) -> List[str]:
        """List all registered agent names."""
        return list(self._agents.keys())
    
    def list_workflows(self) -> List[str]:
        """List all registered workflow names."""
        return list(self._workflows.keys())


# Global factory instance
_factory: Optional[FastAgentFactory] = None


def get_factory(config_path: str = "fastagent.config.yaml") -> FastAgentFactory:
    """Get the global FastAgent factory instance."""
    global _factory
    if _factory is None:
        _factory = FastAgentFactory(config_path)
    return _factory


def create_agent_from_config(config: Dict[str, Any]) -> Any:
    """Create a FastAgent from a configuration dictionary.
    
    Args:
        config: Agent configuration with keys:
            - name: Agent name
            - instruction: System prompt
            - model: Model to use
            - temperature: Sampling temperature
            - max_tokens: Max tokens
            - servers: MCP servers
            - workflow: Optional workflow type
            - agents: For workflow patterns
            
    Returns:
        FastAgent instance or workflow
    """
    factory = get_factory()
    
    workflow_type = config.get("workflow")
    
    if workflow_type == "chain":
        return factory.create_chain_workflow(
            name=config.get("name", "chain"),
            sequence=config.get("agents", []),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "parallel":
        return factory.create_parallel_workflow(
            name=config.get("name", "parallel"),
            fan_out=config.get("agents", []),
            fan_in=config.get("fan_in"),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "maker":
        return factory.create_maker_workflow(
            name=config.get("name", "maker"),
            worker=config.get("worker", config.get("agents", [None])[0]),
            k=config.get("k", 3),
            max_samples=config.get("max_samples", 25),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "agents_as_tools":
        return factory.create_agents_as_tools_workflow(
            name=config.get("name", "orchestrator"),
            agents=config.get("agents", []),
            instruction=config.get("instruction", "Use the available agents."),
            servers=config.get("servers"),
        )
    else:
        # Regular agent
        return factory.create_agent(
            name=config.get("name", "agent"),
            instruction=config.get("instruction", "You are a helpful assistant."),
            model=config.get("model", "sonnet"),
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens"),
            servers=config.get("servers"),
            human_input=config.get("human_input", False),
            tools=config.get("tools"),
        )


__all__ = [
    "FastAgentFactory",
    "create_agent_from_config",
    "get_factory",
    "FASTAGENT_AVAILABLE",
]
