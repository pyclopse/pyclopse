"""Parallel workflow pattern - fan-out/fan-in execution."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable

from pyclopse.agents.factory import get_factory, FASTAGENT_AVAILABLE


logger = logging.getLogger("pyclopse.workflows.parallel")


@dataclass
class ParallelAgent:
    """An agent in a parallel workflow."""
    name: str
    instruction: str
    agent_name: Optional[str] = None
    input_transform: Optional[Callable[[Any], str]] = None


class ParallelWorkflow:
    """Parallel workflow - executes agents concurrently (fan-out/fan-in).
    
    Multiple agents process the same input simultaneously,
    then results are aggregated.
    """
    
    def __init__(
        self,
        name: str,
        agents: List[ParallelAgent],
        fan_in_agent: Optional[str] = None,
        fan_in_instruction: str = "Aggregate the following results:",
        model: str = "sonnet",
        temperature: float = 0.7,
        max_concurrent: int = 5,
    ):
        """Initialize parallel workflow.
        
        Args:
            name: Workflow name
            agents: List of agents to run in parallel
            fan_in_agent: Optional agent to aggregate results
            fan_in_instruction: Instruction for fan-in agent
            model: Model to use
            temperature: Sampling temperature
            max_concurrent: Maximum concurrent executions
        """
        self.name = name
        self.agents = agents
        self.fan_in_agent = fan_in_agent
        self.fan_in_instruction = fan_in_instruction
        self.model = model
        self.temperature = temperature
        self.max_concurrent = max_concurrent
        
        self._factory = get_factory()
        self._results: Dict[str, Any] = {}
    
    async def _run_agent(
        self,
        agent_config: ParallelAgent,
        input_data: Any,
    ) -> tuple[str, Any]:
        """Run a single agent.
        
        Args:
            agent_config: Agent configuration
            input_data: Input to process
            
        Returns:
            Tuple of (agent_name, result)
        """
        agent_name = agent_config.agent_name or agent_config.name
        
        # Transform input if needed
        prompt = input_data
        if agent_config.input_transform:
            prompt = agent_config.input_transform(input_data)
        
        # Get or create agent
        agent = self._factory.get_agent(agent_name)
        if agent is None:
            agent = self._factory.create_agent(
                name=agent_name,
                instruction=agent_config.instruction,
                model=self.model,
                temperature=self.temperature,
            )
        
        # Execute agent
        async with agent.run() as agent_instance:
            result = await agent_instance(prompt)
        
        logger.info(f"Parallel agent {agent_name} completed")
        
        return agent_name, result
    
    async def run(self, input_data: Any) -> Any:
        """Run the parallel workflow.
        
        Args:
            input_data: Input for all parallel agents
            
        Returns:
            Aggregated results or list of individual results
        """
        if not FASTAGENT_AVAILABLE:
            raise ImportError("FastAgent is not installed")
        
        logger.info(f"Starting parallel workflow: {self.name} with {len(self.agents)} agents")
        
        # Run all agents concurrently with semaphore for limiting
        semaphore = asyncio.Semaphore(self.max_concurrent)
        
        async def run_with_semaphore(agent_config: ParallelAgent):
            async with semaphore:
                return await self._run_agent(agent_config, input_data)
        
        # Execute all agents in parallel
        tasks = [run_with_semaphore(agent) for agent in self.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Agent {self.agents[i].name} failed: {result}")
                self._results[self.agents[i].name] = f"Error: {str(result)}"
            else:
                agent_name, agent_result = result
                self._results[agent_name] = agent_result
        
        # Fan-in: aggregate results if fan-in agent specified
        if self.fan_in_agent:
            logger.info(f"Running fan-in agent: {self.fan_in_agent}")
            
            # Build aggregated input
            aggregated = self.fan_in_instruction + "\n\n"
            for agent_name, result in self._results.items():
                aggregated += f"## {agent_name}\n{result}\n\n"
            
            # Create and run fan-in agent
            fan_in = self._factory.get_agent(self.fan_in_agent)
            if fan_in is None:
                fan_in = self._factory.create_agent(
                    name=self.fan_in_agent,
                    instruction="You are an aggregator. Summarize the results.",
                    model=self.model,
                    temperature=self.temperature,
                )
            
            async with fan_in.run() as agent:
                final_result = await agent(aggregated)
            
            logger.info(f"Parallel workflow {self.name} completed with fan-in")
            return final_result
        
        logger.info(f"Parallel workflow {self.name} completed")
        
        return list(self._results.values())
    
    def get_results(self) -> Dict[str, Any]:
        """Get the individual agent results."""
        return self._results.copy()


async def run_parallel(
    agents: List[Dict[str, Any]],
    input_data: Any,
    fan_in: Optional[Dict[str, Any]] = None,
    model: str = "sonnet",
    temperature: float = 0.7,
    max_concurrent: int = 5,
) -> Any:
    """Convenience function to run a parallel workflow.
    
    Args:
        agents: List of agent configurations
        input_data: Input for all agents
        fan_in: Optional fan-in agent configuration
        model: Model to use
        temperature: Sampling temperature
        max_concurrent: Maximum concurrent executions
        
    Returns:
        Aggregated or individual results
    """
    parallel_agents = [
        ParallelAgent(
            name=agent.get("name", f"agent_{i}"),
            instruction=agent.get("instruction", ""),
            agent_name=agent.get("agent_name"),
            input_transform=agent.get("input_transform"),
        )
        for i, agent in enumerate(agents)
    ]
    
    workflow = ParallelWorkflow(
        name="parallel_workflow",
        agents=parallel_agents,
        fan_in_agent=fan_in.get("name") if fan_in else None,
        fan_in_instruction=fan_in.get("instruction", "Aggregate:") if fan_in else None,
        model=model,
        temperature=temperature,
        max_concurrent=max_concurrent,
    )
    
    return await workflow.run(input_data)


__all__ = [
    "ParallelAgent",
    "ParallelWorkflow",
    "run_parallel",
]
