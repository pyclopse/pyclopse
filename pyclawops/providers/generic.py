"""Generic OpenAI-compatible provider."""

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from . import ChatResponse, Message, Provider, ToolCall


class GenericProvider(Provider):
    """Provider for any OpenAI-compatible API endpoint.

    Used for providers like MiniMax, z.ai, Groq, or any other endpoint
    that speaks the OpenAI chat completions protocol.  Credentials and
    the base URL come entirely from config — no provider-specific logic.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_url = config.get("api_url") or config.get("apiUrl") or None
        self.base_url = self.api_url
        self.model = config.get("model", "")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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
        if not self.api_key:
            raise ValueError("api_key is required — set it in the provider config")
        if not self.base_url:
            raise ValueError("api_url is required — set it in the provider config")

        model = model or self.model
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        content = choice["message"].get("content", "")
        return ChatResponse(
            content=content,
            model=data.get("model", model),
            usage={"total_tokens": data.get("usage", {}).get("total_tokens", 0)},
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
        if not self.api_key:
            raise ValueError("api_key is required")
        if not self.base_url:
            raise ValueError("api_url is required")

        model = model or self.model
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._get_headers(),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip() or not line.startswith("data:"):
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
        raise NotImplementedError("Embeddings not implemented in GenericProvider")

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return False
