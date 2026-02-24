"""FastAgent provider wrapper for multi-provider support."""

import os
from typing import Any, AsyncIterator, Dict, List, Optional

from . import ChatResponse, Message, Provider, ToolCall


class FastAgentProvider(Provider):
    """Provider for FastAgent (multi-provider gateway)."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.url = config.get("url", "http://localhost:8000")
        self._client = None
    
    @property
    def client(self):
        """Lazy-load httpx client."""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(base_url=self.url, timeout=60.0)
        return self._client
    
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
        payload = {
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
            ],
            "model": model or self.default_model,
            "temperature": temperature,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        
        response = await self.client.post("/v1/chat/completions", json=payload)
        response.raise_for_status()
        
        data = response.json()
        choice = data["choices"][0]
        
        content = choice["message"].get("content", "")
        tool_calls = None
        
        if choice["message"].get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in choice["message"]["tool_calls"]
            ]
        
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            model=data.get("model", "fastagent"),
            usage=data.get("usage"),
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
        payload = {
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
            ],
            "model": model or self.default_model,
            "temperature": temperature,
            "stream": True,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        
        async with self.client.stream("POST", "/v1/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for chunk in response.aiter_lines():
                if chunk.startswith("data: "):
                    data = chunk[6:]
                    if data == "[DONE]":
                        break
                    import json
                    try:
                        delta = json.loads(data)["choices"][0]["delta"]
                        if delta.get("content"):
                            yield delta["content"]
                    except (json.JSONDecodeError, KeyError):
                        pass
    
    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """Get embeddings."""
        payload = {
            "input": text,
            "model": model or "text-embedding-3-small",
        }
        
        response = await self.client.post("/v1/embeddings", json=payload)
        response.raise_for_status()
        
        data = response.json()
        return data["data"][0]["embedding"]
    
    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        return True
    
    @property
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        return True


__all__ = ["FastAgentProvider"]
