"""FastAgent provider wrapper - connects pyclawops to FastAgent server."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from . import ChatResponse, Message, Provider, ToolCall

logger = logging.getLogger("pyclawops.providers.fastagent")


class FastAgentProvider(Provider):
    """Provider that connects to a FastAgent server.
    
    This provider acts as a client to a FastAgent server instance,
    which handles the actual LLM calls and workflow execution.
    
    For direct FastAgent integration within pyclawops, use the
    pyclawops.agents module instead.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize FastAgent provider.
        
        Args:
            config: Configuration with keys:
                - url: FastAgent server URL (default: http://localhost:8000)
                - default_model: Model to use (default: sonnet)
                - api_key: Optional API key
        """
        super().__init__(config)
        self.url = config.get("url", "http://localhost:8000")
        self._client = None
        
        # Check if FastAgent is available for direct integration
        try:
            from fast_agent import FastAgent
            self._has_fastagent = True
        except ImportError:
            self._has_fastagent = False
    
    @property
    def client(self):
        """Lazy-load httpx client."""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self.url,
                timeout=60.0,
                headers=self._get_headers(),
            )
        return self._client
    
    def _get_headers(self) -> Dict[str, str]:
        """Get request headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
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
            "model": model or self.default_model or "sonnet",
            "temperature": temperature,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        
        # Add any additional kwargs
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        
        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"FastAgent request failed: {e}")
            return ChatResponse(
                content=f"Error: {str(e)}",
                model=model or "unknown",
            )
        
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
            "model": model or self.default_model or "sonnet",
            "temperature": temperature,
            "stream": True,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        
        # Add any additional kwargs
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        
        try:
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
        except Exception as e:
            logger.error(f"FastAgent stream error: {e}")
            yield f"Error: {str(e)}"
    
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
        
        try:
            response = await self.client.post("/v1/embeddings", json=payload)
            response.raise_for_status()
            
            data = response.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"FastAgent embed error: {e}")
            return []
    
    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        return True
    
    @property
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        return True
    
    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


class DirectFastAgentProvider(Provider):
    """Direct FastAgent integration (no server required).
    
    This provider uses FastAgent directly within pyclawops,
    bypassing the need for a separate FastAgent server.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize direct FastAgent provider.
        
        Args:
            config: Configuration with keys:
                - default_model: Model to use
                - temperature: Default temperature
                - servers: MCP server names
        """
        super().__init__(config)
        
        try:
            from fast_agent import FastAgent
            self._FastAgent = FastAgent
            self._fast = None
            self._agent = None
        except ImportError as e:
            raise ImportError(
                "FastAgent not installed. Install with: uv pip install fast-agent-mcp"
            ) from e
        
        self.default_model = config.get("default_model", "sonnet")
        self.temperature = config.get("temperature", 0.7)
        self.servers = config.get("servers", [])
    
    async def _ensure_agent(self):
        """Ensure agent is initialized."""
        if self._agent is None:
            self._fast = self._FastAgent("pyclawops-provider")
            
            @self._fast.agent(
                name="assistant",
                instruction="You are a helpful assistant.",
                servers=self.servers,
            )
            async def main():
                async with self._fast.run() as agent:
                    await agent.interactive()
            
            self._agent = self._fast
    
    async def chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        """Send a chat completion request directly to FastAgent."""
        await self._ensure_agent()
        
        # Build prompt from messages
        prompt = self._messages_to_prompt(messages)
        
        try:
            async with self._fast.run() as agent:
                result = await agent(prompt)
                return ChatResponse(
                    content=str(result),
                    model=model or self.default_model,
                )
        except Exception as e:
            logger.error(f"Direct FastAgent error: {e}")
            return ChatResponse(
                content=f"Error: {str(e)}",
                model=model or self.default_model,
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
        await self._ensure_agent()
        
        prompt = self._messages_to_prompt(messages)
        
        try:
            async with self._fast.run() as agent:
                async for chunk in agent.stream(prompt):
                    yield str(chunk)
        except Exception as e:
            logger.error(f"Direct FastAgent stream error: {e}")
            yield f"Error: {str(e)}"
    
    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """Get embeddings (not directly supported, returns empty)."""
        logger.warning("Embeddings not directly supported in DirectFastAgentProvider")
        return []
    
    def _messages_to_prompt(self, messages: List[Message]) -> str:
        """Convert messages to a single prompt string."""
        parts = []
        for msg in messages:
            if msg.role == "system":
                parts.insert(0, msg.content)
            else:
                parts.append(f"{msg.role.upper()}: {msg.content}")
        return "\n\n".join(parts)
    
    @property
    def supports_streaming(self) -> bool:
        return True
    
    @property
    def supports_tools(self) -> bool:
        return True


__all__ = ["FastAgentProvider", "DirectFastAgentProvider"]
