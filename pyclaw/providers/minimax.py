"""MiniMax provider for GPT models."""

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from . import ChatResponse, Message, Provider, ToolCall


class MiniMaxProvider(Provider):
    """Provider for MiniMax's API."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Get API key from config, environment, or Keychain
        api_key = self.api_key or os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            # Try to get from Keychain
            try:
                import subprocess
                api_key = subprocess.check_output(
                    ["security", "find-generic-password", "-s", "pyclaw", "-a", "minimax-api-key", "-w"],
                    text=True
                ).strip()
            except:
                pass
        
        # Store key (may be None - raises lazily when chat() is called)
        self.base_url = config.get("base_url", "https://api.minimax.io/v1/text/chatcompletion_v2")
        self.api_key = api_key
        self.model = config.get("model", "MiniMax-M2.5")
        self.default_model = self.model
    
    def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers for API requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

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
        if not self.api_key:
            raise ValueError("MINIMAX_API_KEY is required - set in config, env, or Keychain")
        model = model or self.default_model or "MiniMax-M2.5"

        headers = self._get_headers()
        
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        
        if max_tokens:
            payload["max_tokens"] = max_tokens
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.base_url,
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            
            # Check for API-level errors
            if "base_resp" in data:
                err = data["base_resp"]
                raise Exception(f"MiniMax API error: {err.get('status_msg', 'Unknown')} (code: {err.get('status_code')})")
            
            choice = data["choices"][0]
            msg = choice["message"]
            
            content = msg.get("content", "")
            tool_calls = None
            
            return ChatResponse(
                content=content,
                tool_calls=tool_calls,
                model=data.get("model", model),
                usage={"total_tokens": data.get("usage", {}).get("total_tokens", 0)}
            )
    
    async def chat_stream(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        """Send a streaming chat completion request using SSE."""
        model = model or self.default_model
        
        # Build messages payload
        messages_payload = []
        for msg in messages:
            msg_dict = {"role": msg.role, "content": msg.content}
            if msg.name:
                msg_dict["name"] = msg.name
            messages_payload.append(msg_dict)
        
        # Build request
        payload = {
            "model": model,
            "messages": messages_payload,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", self.base_url, json=payload, headers=self._get_headers()) as response:
                response.raise_for_status()
                
                # Check if response is SSE streaming or a regular JSON response
                content_type = response.headers.get("content-type", "")
                
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    
                    # Check for error responses (non-SSE format)
                    if not line.startswith("data:"):
                        # Try to parse as JSON to check for errors
                        try:
                            data = json.loads(line)
                            if "base_resp" in data:
                                err = data["base_resp"]
                                raise Exception(f"MiniMax API error: {err.get('status_msg', 'Unknown')} (code: {err.get('status_code')})")
                        except json.JSONDecodeError:
                            pass
                        continue
                    
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    
                    try:
                        data = json.loads(data_str)
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield ChatResponse(
                                    content=content,
                                    finish_reason=choices[0].get("finish_reason"),
                                    model=data.get("model", model),
                                )
                    except json.JSONDecodeError:
                        continue
    
    async def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """Get embeddings for text."""
        # MiniMax may not support embeddings in the coding plan
        raise NotImplementedError("Embeddings not supported in MiniMax coding plan")
    
    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        return True  # Now supports real streaming
    
    @property
    def supports_tools(self) -> bool:
        """Whether this provider supports tool calls."""
        return False  # Simplified
