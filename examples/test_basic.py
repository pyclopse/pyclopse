"""Basic test script for pyclopse.

This script demonstrates:
1. Loading config
2. Creating a simple test agent
3. Running one turn
4. Printing the result
"""

import asyncio
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from pyclopse.config.loader import ConfigLoader
from pyclopse.core.session import Session
from pyclopse.core.router import IncomingMessage, OutgoingMessage
from pyclopse.providers import Message as ProviderMessage
from pyclopse.providers import ChatResponse


class MockProvider:
    """Simple mock provider for testing."""
    
    def __init__(self, model: str = "mock/test"):
        self.model = model
    
    async def chat(
        self,
        messages: List[ProviderMessage],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        """Return a mock response."""
        # Get the last user message
        user_content = "Hello"
        for msg in reversed(messages):
            if msg.role == "user":
                user_content = msg.content
                break
        return ChatResponse(
            content=f"Echo: {user_content}",
            model=model or self.model,
        )
    
    async def chat_stream(self, *args, **kwargs):
        """Stream not supported in mock."""
        yield "Mock streaming not supported"
    """Simple mock agent for testing."""
    id: str
    name: str
    is_running: bool = True
    
    async def handle_message(self, message: IncomingMessage, session: Session) -> Optional[OutgoingMessage]:
        """Handle a message and return response."""
        # Simple echo response
        return OutgoingMessage(
            id=f"resp_{message.id}",
            content=f"[Mock] Received: {message.content}",
            channel=message.channel,
            recipient=message.sender_id,
        )


class MockSessionManager:
    """Mock session manager for testing."""
    
    async def get_or_create_session(self, agent_id: str, channel: str, user_id: str) -> Session:
        """Get or create a session."""
        return Session(
            id=f"session_{channel}_{user_id}",
            agent_id=agent_id,
            channel=channel,
            user_id=user_id,
            is_active=True,
        )


async def main():
    """Run the basic test."""
    print("=" * 50)
    print("Pyclopse Basic Test")
    print("=" * 50)
    
    # 1. Load config
    print("\n[1] Loading config...")
    config_path = "~/.pyclopse/config.yaml"
    loader = ConfigLoader(config_path)
    
    try:
        config = loader.load()
        print(f"    ✓ Config loaded: version {config.version}")
        print(f"    ✓ Gateway: {config.gateway.host}:{config.gateway.port}")
    except FileNotFoundError:
        print(f"    ✗ Config not found at {config_path}")
        print("    (This is OK for testing - using defaults)")
        config = None
    except Exception as e:
        print(f"    ✗ Config error: {e}")
        config = None
    
    # 2. Create a simple test agent
    print("\n[2] Creating test agent...")
    
    # Using the real Agent class with minimal setup
    from pyclopse.core.agent import Agent
    from pyclopse.config.schema import AgentConfig, ToolsConfig
    
    agent_config = AgentConfig(
        name="TestAgent",
        model="mock/echo",
        system_prompt="You are a test agent.",
        max_tokens=1024,
        temperature=0.7,
        tools=ToolsConfig(enabled=False),  # Disable tools for simple test
    )
    
    session_manager = MockSessionManager()
    
    agent = Agent(
        id="test-agent",
        name="TestAgent",
        config=agent_config,
        session_manager=session_manager,
    )
    
    # Inject mock provider for testing
    agent.provider = MockProvider(model="mock/echo")
    
    print(f"    ✓ Agent created: {agent.name} (id: {agent.id})")
    
    # 3. Run one turn
    print("\n[3] Running one turn...")
    
    # Create a test session
    session = await session_manager.get_or_create_session(
        agent_id=agent.id,
        channel="test",
        user_id="test_user",
    )
    print(f"    ✓ Session created: {session.id}")
    
    # Create test message
    test_message = IncomingMessage(
        id="test_msg_1",
        content="Hello, agent!",
        channel="test",
        sender="test_user",
        sender_id="test_user",
    )
    
    # Handle the message
    response = await agent.handle_message(test_message, session)
    
    # 4. Print result
    print("\n[4] Result:")
    print("-" * 50)
    if response:
        print(f"    Response: {response.content}")
    else:
        print("    No response received")
    print("-" * 50)
    
    print("\n✓ Test completed successfully!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
