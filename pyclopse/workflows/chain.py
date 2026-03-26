"""Chain workflow pattern - sequential execution."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable

from pyclopse.agents.factory import get_factory, FASTAGENT_AVAILABLE


logger = logging.getLogger("pyclopse.workflows.chain")


@dataclass
class ChainStep:
    """A step in a chain workflow."""
    name: str
    instruction: str
    agent_name: Optional[str] = None
    input_mapping: Optional[Dict[str, str]] = None
    output_key: str = "output"
    transform: Optional[Callable[[Any], Any]] = None


class ChainWorkflow:
    """Chain workflow - executes agents sequentially.
    
    Each agent's output becomes the next agent's input.
    """
    
    def __init__(
        self,
        name: str,
        steps: List[ChainStep],
        model: str = "sonnet",
        temperature: float = 0.7,
    ):
        """Initialize chain workflow.
        
        Args:
            name: Workflow name
            steps: List of chain steps
            model: Model to use
            temperature: Sampling temperature
        """
        self.name = name
        self.steps = steps
        self.model = model
        self.temperature = temperature
        
        self._factory = get_factory()
        self._context: Dict[str, Any] = {}
    
    async def run(self, initial_input: Any) -> Any:
        """Run the chain workflow.
        
        Args:
            initial_input: Input for the first step
            
        Returns:
            Output from the final step
        """
        if not FASTAGENT_AVAILABLE:
            raise ImportError("FastAgent is not installed")
        
        self._context = {"input": initial_input}
        current_input = initial_input
        
        logger.info(f"Starting chain workflow: {self.name}")
        
        for i, step in enumerate(self.steps):
            logger.info(f"Chain step {i + 1}/{len(self.steps)}: {step.name}")
            
            # Map input from context
            if step.input_mapping:
                current_input = self._map_inputs(step.input_mapping)
            elif "last_output" in self._context:
                current_input = self._context["last_output"]
            
            # Transform if needed
            if step.transform:
                current_input = step.transform(current_input)
            
            # Create and run agent for this step
            agent_name = step.agent_name or step.name
            
            # Check if agent exists in factory
            agent = self._factory.get_agent(agent_name)
            if agent is None:
                # Create a new agent for this step
                agent = self._factory.create_agent(
                    name=agent_name,
                    instruction=step.instruction,
                    model=self.model,
                    temperature=self.temperature,
                )
            
            # Execute the agent
            async with agent.run() as agent_instance:
                result = await agent_instance(current_input)
            
            # Store result in context
            self._context[step.output_key] = result
            self._context["last_output"] = result
            
            logger.info(f"Chain step {step.name} completed")
        
        logger.info(f"Chain workflow {self.name} completed")
        
        return self._context.get("last_output", "")
    
    def _map_inputs(self, mapping: Dict[str, str]) -> Any:
        """Map inputs from context based on mapping rules.
        
        Args:
            mapping: Dict mapping output keys to input keys
            
        Returns:
            Mapped input
        """
        if not mapping:
            return self._context.get("last_output", "")
        
        # Build input from mapped values
        parts = []
        for output_key, input_key in mapping.items():
            value = self._context.get(output_key, "")
            if value:
                parts.append(f"{input_key}: {value}")
        
        return "\n".join(parts) if parts else self._context.get("last_output", "")
    
    def get_context(self) -> Dict[str, Any]:
        """Get the workflow context."""
        return self._context.copy()


async def run_chain(
    steps: List[Dict[str, Any]],
    initial_input: Any,
    model: str = "sonnet",
    temperature: float = 0.7,
) -> Any:
    """Convenience function to run a chain workflow.
    
    Args:
        steps: List of step configurations
        initial_input: Input for first step
        model: Model to use
        temperature: Sampling temperature
        
    Returns:
        Final output from the chain
    """
    chain_steps = [
        ChainStep(
            name=step.get("name", f"step_{i}"),
            instruction=step.get("instruction", ""),
            agent_name=step.get("agent_name"),
            input_mapping=step.get("input_mapping"),
            output_key=step.get("output_key", "output"),
        )
        for i, step in enumerate(steps)
    ]
    
    workflow = ChainWorkflow(
        name="chain_workflow",
        steps=chain_steps,
        model=model,
        temperature=temperature,
    )
    
    return await workflow.run(initial_input)


__all__ = [
    "ChainStep",
    "ChainWorkflow",
    "run_chain",
]
