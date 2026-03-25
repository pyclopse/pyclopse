"""Agents-as-tools workflow pattern - orchestrator with child agents as tools."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable

from pyclawops.agents.factory import get_factory, FASTAGENT_AVAILABLE


logger = logging.getLogger("pyclawops.workflows.agents_as_tools")


@dataclass
class ChildAgent:
    """A child agent exposed as a tool."""
    name: str
    instruction: str
    agent_name: Optional[str] = None
    servers: List[str] = field(default_factory=list)


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator agent."""
    name: str
    instruction: str
    child_agents: List[ChildAgent]
    default: bool = True
    servers: List[str] = field(default_factory=list)


class AgentsAsToolsWorkflow:
    """Agents-as-tools workflow - orchestrator uses child agents as tools.
    
    This pattern enables:
    - Routing: Choose the right specialist based on input
    - Parallelization: Fan out to independent agents
    - Orchestrator-workers: Decompose task into subtasks
    """
    
    def __init__(
        self,
        name: str,
        orchestrator: OrchestratorConfig,
        model: str = "sonnet",
        temperature: float = 0.7,
    ):
        """Initialize agents-as-tools workflow.
        
        Args:
            name: Workflow name
            orchestrator: Orchestrator configuration
            model: Model to use
            temperature: Sampling temperature
        """
        self.name = name
        self.orchestrator = orchestrator
        self.model = model
        self.temperature = temperature
        
        self._factory = get_factory()
        self._call_history: List[Dict[str, Any]] = []
    
    async def _create_child_agents(self) -> List[str]:
        """Create all child agents.
        
        Returns:
            List of child agent names
        """
        child_names = []
        
        for child in self.orchestrator.child_agents:
            agent_name = child.agent_name or child.name
            
            # Check if already exists
            if self._factory.get_agent(agent_name) is None:
                self._factory.create_agent(
                    name=agent_name,
                    instruction=child.instruction,
                    model=self.model,
                    temperature=self.temperature,
                    servers=child.servers,
                )
            
            child_names.append(agent_name)
            logger.info(f"Created child agent: {agent_name}")
        
        return child_names
    
    async def _create_orchestrator(self, child_names: List[str]):
        """Create the orchestrator agent with child agents as tools.
        
        Args:
            child_names: Names of child agents to expose as tools
        """
        # Build instruction with available tools
        tool_list = "\n".join([
            f"- {name}: {child.instruction[:100]}..."
            for name, child in zip(
                child_names,
                self.orchestrator.child_agents
            )
        ])
        
        full_instruction = f"""{self.orchestrator.instruction}

Available tools (agents):
{tool_list}

When a task requires a specific agent, call them as tools.
"""
        
        orchestrator_name = self.orchestrator.agent_name or self.orchestrator.name
        
        self._factory.create_agents_as_tools_workflow(
            name=orchestrator_name,
            agents=child_names,
            instruction=full_instruction,
            default=self.orchestrator.default,
            servers=self.orchestrator.servers,
        )
        
        logger.info(f"Created orchestrator: {orchestrator_name}")
    
    async def run(self, prompt: str) -> Any:
        """Run the agents-as-tools workflow.
        
        Args:
            prompt: User prompt
            
        Returns:
            Orchestrator response
        """
        if not FASTAGENT_AVAILABLE:
            raise ImportError("FastAgent is not installed")
        
        logger.info(f"Starting agents-as-tools workflow: {self.name}")
        
        # Create child agents
        child_names = await self._create_child_agents()
        
        # Create orchestrator with child agents as tools
        await self._create_orchestrator(child_names)
        
        # Get the orchestrator agent
        orchestrator_name = self.orchestrator.agent_name or self.orchestrator.name
        orchestrator = self._factory.get_agent(orchestrator_name)
        
        if orchestrator is None:
            raise ValueError(f"Orchestrator not created: {orchestrator_name}")
        
        # Execute the orchestrator
        async with orchestrator.run() as agent:
            result = await agent(prompt)
        
        logger.info(f"Agents-as-tools workflow {self.name} completed")
        
        return result
    
    async def run_with_plan(
        self,
        prompt: str,
        plan: List[Dict[str, Any]],
    ) -> Any:
        """Run with a predefined plan.
        
        This executes a pre-determined plan instead of letting
        the orchestrator decide which agents to call.
        
        Args:
            prompt: Initial prompt
            plan: List of agent calls to make
            
        Returns:
            Final result after executing plan
        """
        if not FASTAGENT_AVAILABLE:
            raise ImportError("FastAgent is not installed")
        
        logger.info(f"Running agents-as-tools with plan: {len(plan)} steps")
        
        # Create child agents
        child_names = await self._create_child_agents()
        
        context = {"initial_input": prompt, "results": {}}
        current_input = prompt
        
        for i, step in enumerate(plan):
            agent_name = step.get("agent")
            agent_input = step.get("input", current_input)
            
            # Get or create agent
            agent = self._factory.get_agent(agent_name)
            if agent is None:
                # Find instruction from plan or use default
                instruction = step.get("instruction", f"You are {agent_name}.")
                agent = self._factory.create_agent(
                    name=agent_name,
                    instruction=instruction,
                    model=self.model,
                    temperature=self.temperature,
                )
            
            # Execute agent
            async with agent.run() as agent_instance:
                result = await agent_instance(agent_input)
            
            # Store result
            context["results"][agent_name] = result
            current_input = result
            
            logger.info(f"Plan step {i + 1}/{len(plan)}: {agent_name} completed")
        
        return context.get("results", {}).get(
            list(context["results"].keys())[-1], 
            ""
        )
    
    def get_call_history(self) -> List[Dict[str, Any]]:
        """Get the history of agent calls."""
        return self._call_history.copy()


async def run_agents_as_tools(
    orchestrator: Dict[str, Any],
    prompt: str,
    model: str = "sonnet",
    temperature: float = 0.7,
    use_plan: bool = False,
    plan: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Convenience function to run an agents-as-tools workflow.
    
    Args:
        orchestrator: Orchestrator configuration
        prompt: User prompt
        model: Model to use
        temperature: Sampling temperature
        use_plan: Whether to use a predefined plan
        plan: Optional predefined plan
        
    Returns:
        Workflow result
    """
    child_agents = [
        ChildAgent(
            name=child.get("name", f"child_{i}"),
            instruction=child.get("instruction", ""),
            agent_name=child.get("agent_name"),
            servers=child.get("servers", []),
        )
        for i, child in enumerate(orchestrator.get("child_agents", []))
    ]
    
    orchestrator_config = OrchestratorConfig(
        name=orchestrator.get("name", "orchestrator"),
        instruction=orchestrator.get("instruction", "Use the available agents."),
        child_agents=child_agents,
        default=orchestrator.get("default", True),
        servers=orchestrator.get("servers", []),
    )
    
    workflow = AgentsAsToolsWorkflow(
        name=orchestrator.get("name", "agents_as_tools"),
        orchestrator=orchestrator_config,
        model=model,
        temperature=temperature,
    )
    
    if use_plan and plan:
        return await workflow.run_with_plan(prompt, plan)
    
    return await workflow.run(prompt)


__all__ = [
    "ChildAgent",
    "OrchestratorConfig", 
    "AgentsAsToolsWorkflow",
    "run_agents_as_tools",
]
