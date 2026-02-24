"""MiniMax provider for GPT models."""

import os
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from . import ChatResponse, Message, Provider, ToolCall


class MiniMaxProvider(Provider):
    """Provider for MiniMax's API (OpenAI-compatible)."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Get API key from config or environment
        api_key = self.api_key or os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise ValueError("MINIMAX_API_KEY is required")
        
        base_url = config.get("base_url", "https://api.minimax.chat/v1")
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Convert internal messages to MiniMax format."""
        converted = []
        for msg in messages:
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
            }
            
            # Add tool calls if present
            if msg.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": str(tc.arguments),  # JSON string
                        },
                    }
                    for tc in msg.tool_calls
                ]
            
            # Add tool call id for tool results
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            
            if msg.name:
                msg_dict["name"] = msg.name
            
            converted.append(msg_dict)
        
        return converted
    
    def _convert_tools(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        """Convert tools to OpenAI function calling format."""
        if not tools:
            return None
        
        converted = []
        for tool in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
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
        """Send a non-streaming chat completion request."""
        model = model or self.default_model or "MiniMax-M2.5"
        
        minimax_messages = self._convert_messages(messages)
        minimax_tools = self._convert_tools(tools)
        
        extra_kwargs = {}
        if minimax_tools:
            extra_kwargs["tools"] = minimax_tools
        
        response = await self._client.chat.completions.create(
            model=model,
            messages=minimax_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_kwargs,
        )
        
        choice = response.choices[0]
        message = choice.message
        
        # Extract content
        content = message.content or ""
        
        # Extract tool calls
        tool_calls = None
        if message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=eval(tc.function.arguments),  # Parse JSON string to dict
                )
                for tc in message.tool_calls
            ]
        
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
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
        model = model or self.default_model or "MiniMax-M2.5"
        
        minimax_messages = self._convert_messages(messages)
        minimax_tools = self._convert_tools(tools)
        
        extra_kwargs = {}
        if minimax_tools:
            extra_kwargs["tools"] = minimax_tools
        
        stream = await self._client.chat.completions.create(
            model=model,
            messages=minimax_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **extra_kwargs,
        )
        
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    
    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """Get embeddings for text."""
        # MiniMax uses embo-1 for embeddings
        model = model or "embo-1"
        
        response = await self._client.embeddings.create(
            model=model,
            input=text,
        )
        
        return response.data[0].embedding
    
    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        return True
    
    @property
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        return True


# Export for easy importing
__all__ = ["MiniMaxProvider"]
