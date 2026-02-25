"""Agent runner using FastAgent."""
import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from pyclaw.agents.factory import AgentFactory

logger = logging.getLogger(__name__)


class AgentRunner:
    """Runner for FastAgent-based execution."""
    
    def __init__(
        self,
        agent_name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[Dict[str, Any]]] = None,
    ):
        self.agent_name = agent_name
        self.instruction = instruction
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.servers = servers or []
        self._app: Optional[Any] = None
        self._message_history: List[Dict[str, str]] = []
    
    async def initialize(self):
        """Initialize the FastAgent app."""
        if self._app is not None:
            return
            
        from fast_agent import FastAgent
        
        # Create FastAgent and run it to get the app
        fast = FastAgent(self.agent_name)
        
        @fast.agent(
            instruction=self.instruction,
            model=self.model,
        )
        async def main():
            pass
        
        # Run to get the app instance
        async with fast.run() as app:
            self._app = app
            logger.info(f"Initialized agent runner: {self.agent_name}")
    
    async def run(self, prompt: str) -> str:
        """Run a single prompt through the agent.
        
        Args:
            prompt: User prompt
            
        Returns:
            Agent response content
        """
        if self._app is None:
            await self.initialize()
        
        # Add to history
        self._message_history.append({"role": "user", "content": prompt})
        
        # Send message via app
        result = await self._app.send(prompt)
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
        if self._app is None:
            await self.initialize()
        
        self._message_history.append({"role": "user", "content": prompt})
        
        # For streaming, we'd need to use a different method
        # For now, fall back to non-streaming
        result = await self._app.send(prompt)
        yield str(result)
    
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
        if self._app is None:
            await self.initialize()
        
        # Build conversation
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                await self._app.send(content)
        
        # Get final response
        return self._message_history[-1]["content"] if self._message_history else ""
    
    def get_history(self) -> List[Dict[str, str]]:
        """Get message history."""
        return self._message_history.copy()


async def run_agent_turn(
    agent_config: Dict[str, Any],
    messages: List[Dict[str, str]],
) -> str:
    """Run a single agent turn.
    
    Args:
        agent_config: Agent configuration
        messages: List of messages
        
    Returns:
        Agent response
    """
    runner = AgentRunner(
        agent_name=agent_config.get("name", "agent"),
        instruction=agent_config.get("instruction", ""),
        model=agent_config.get("model", "sonnet"),
        temperature=agent_config.get("temperature", 0.7),
        max_tokens=agent_config.get("max_tokens"),
        servers=agent_config.get("servers", []),
    )
    
    return await runner.run_with_history(messages)
