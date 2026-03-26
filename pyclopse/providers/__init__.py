"""Provider abstraction layer for pyclopse."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class Message:
    """A message in a conversation."""
    role: str  # system, user, assistant
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # For tool results


@dataclass
class ToolCall:
    """A tool call from the model."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    """Result from a tool execution."""
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ChatResponse:
    """Response from a chat completion."""
    content: str
    model: str
    tool_calls: Optional[List[ToolCall]] = None
    usage: Optional[Dict[str, int]] = None


class Provider(ABC):
    """Base class for model providers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_key = config.get("api_key")
        self.default_model = config.get("default_model")
        self.fastagent_provider = config.get("fastagent_provider")
    
    @abstractmethod
    async def chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        """Send a chat completion request."""
        pass
    
    @abstractmethod
    async def chat_stream(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream chat completion responses."""
        pass
    
    @abstractmethod
    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """Get embeddings for text."""
        pass
    
    @property
    @abstractmethod
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        pass
    
    @property
    @abstractmethod
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        pass


class ProviderRegistry:
    """Registry for managing providers."""
    
    def __init__(self):
        self._providers: Dict[str, Provider] = {}
        self._default_provider: Optional[str] = None
    
    def register(self, name: str, provider: Provider, set_default: bool = False) -> None:
        """Register a provider."""
        self._providers[name] = provider
        if set_default or self._default_provider is None:
            self._default_provider = name
    
    def get(self, name: str) -> Optional[Provider]:
        """Get a provider by name."""
        return self._providers.get(name)
    
    def get_default(self) -> Optional[Provider]:
        """Get the default provider."""
        if self._default_provider:
            return self._providers.get(self._default_provider)
        return None
    
    def set_default(self, name: str) -> bool:
        """Set the default provider."""
        if name in self._providers:
            self._default_provider = name
            return True
        return False
    
    def list_providers(self) -> List[str]:
        """List all registered provider names."""
        return list(self._providers.keys())


# Global registry instance
_registry = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    """Get the global provider registry."""
    return _registry


def create_provider(provider_type: str, config: Dict[str, Any]) -> Provider:
    """Factory function to create a provider by type."""
    if provider_type == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(config)
    elif provider_type == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider(config)
    elif provider_type == "fastagent":
        from .fastagent import FastAgentProvider
        return FastAgentProvider(config)
    elif provider_type == "minimax" or config.get("fastagent_provider"):
        # Any OpenAI-compatible provider (minimax, zai, groq, etc.)
        from .generic import GenericProvider
        return GenericProvider(config)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


def register_provider(name: str):
    """Decorator to register a provider."""
    def decorator(cls):
        # Will be registered after instantiation
        return cls
    return decorator


# Export public types
__all__ = [
    "Message",
    "ToolCall", 
    "ToolResult",
    "ChatResponse",
    "Provider",
    "ProviderRegistry",
    "get_registry",
    "create_provider",
    "register_provider",
]
