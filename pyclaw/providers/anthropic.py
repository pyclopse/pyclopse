"""Anthropic provider for Claude models."""

import os
from typing import Any, AsyncIterator, Dict, List, Optional

from . import ChatResponse, Message, Provider, ToolCall


class AnthropicProvider(Provider):
    """Provider for Anthropic's Claude models."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialise the Anthropic provider, validating that an API key is available.

        Args:
            config (Dict[str, Any]): Provider configuration dict. The ``api_key``
                entry (or the ``ANTHROPIC_API_KEY`` environment variable) must be
                present.

        Raises:
            ValueError: If no API key is found in config or the environment.
        """
        super().__init__(config)
        # Get API key from config or environment
        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        
        self._client = None  # Lazy initialization
    
    @property
    def client(self):
        """Lazy-load the Anthropic async client.

        Returns:
            anthropic.AsyncAnthropic: The shared Anthropic client instance,
                constructed on the first access.
        """
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                max_retries=3,
            )
        return self._client
    
    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Convert internal message objects to the Anthropic API message format.

        System role messages are re-emitted as assistant turns prefixed with
        ``"System: "`` because Anthropic's messages API does not accept a
        ``system`` role inside the messages array.

        Args:
            messages (List[Message]): Sequence of internal :class:`Message`
                objects to convert.

        Returns:
            List[Dict[str, Any]]: List of message dicts ready for the Anthropic
                ``messages.create`` call.
        """
        converted = []
        for msg in messages:
            if msg.role == "system":
                # Anthropic handles system messages differently
                converted.append({
                    "role": "assistant",
                    "content": f"\n\nSystem: {msg.content}"
                })
            else:
                converted.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        return converted
    
    def _convert_tools(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        """Convert generic tool definitions to the Anthropic tool schema format.

        Args:
            tools (Optional[List[Dict[str, Any]]]): Generic tool list where each
                entry has ``name``, ``description``, and ``parameters`` keys.

        Returns:
            Optional[List[Dict[str, Any]]]: Tools formatted with Anthropic's
                ``input_schema`` key, or ``None`` if ``tools`` is empty/None.
        """
        if not tools:
            return None
        
        converted = []
        for tool in tools:
            converted_tool = {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            converted.append(converted_tool)
        return converted
    
    async def chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        """Send a non-streaming chat completion request to the Anthropic API.

        Args:
            messages (List[Message]): Conversation history to send.
            model (Optional[str]): Model identifier. Defaults to the provider's
                ``default_model`` or ``claude-3-5-sonnet-20241022``.
            tools (Optional[List[Dict[str, Any]]]): Tool definitions to make
                available to the model.
            temperature (float): Sampling temperature. Defaults to 0.7.
            max_tokens (Optional[int]): Maximum tokens to generate. Defaults to
                4096.
            **kwargs: Additional keyword arguments (ignored).

        Returns:
            ChatResponse: Completed response including content, any tool calls,
                the resolved model name, and token usage statistics.
        """
        model = model or self.default_model or "claude-3-5-sonnet-20241022"
        max_tokens = max_tokens or 4096
        
        anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)
        
        extra_kwargs = {}
        if anthropic_tools:
            extra_kwargs["tools"] = anthropic_tools
        
        response = await self.client.messages.create(
            model=model,
            messages=anthropic_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_kwargs,
        )
        
        # Extract content
        content = ""
        tool_calls = []
        
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))
        
        return ChatResponse(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )
    
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
        model = model or self.default_model or "claude-3-5-sonnet-20241022"
        max_tokens = max_tokens or 4096
        
        anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools)
        
        extra_kwargs = {}
        if anthropic_tools:
            extra_kwargs["tools"] = anthropic_tools
        
        async with self.client.messages.stream(
            model=model,
            messages=anthropic_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_kwargs,
        ) as stream:
            async for chunk in stream:
                if chunk.type == "content_block_delta":
                    if chunk.delta.type == "text_delta":
                        yield chunk.delta.text
                elif chunk.type == "message_delta":
                    if chunk.delta.stop_reason:
                        # Stream ended
                        pass
    
    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """Get embeddings - Anthropic doesn't have an embeddings API."""
        raise NotImplementedError("Anthropic does not support embeddings")
    
    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        return True
    
    @property
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        return True


# Register the provider
from pyclaw.providers import get_registry

# This will be called when the module is imported
def _register():
    registry = get_registry()
    # Providers will be registered when instantiated with config
    return AnthropicProvider

# Export for easy importing
__all__ = ["AnthropicProvider"]
