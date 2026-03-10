"""
Embedding providers for vector-based memory search.

Providers
---------
openai  — OpenAI Embeddings API (``text-embedding-3-small`` by default).
gemini  — Google Gemini Embeddings (``models/text-embedding-004`` by default).
local   — Any OpenAI-compatible HTTP server (llama.cpp, Ollama, LM Studio).

All providers implement :class:`EmbeddingBackend`:

    async def embed(texts: list[str]) -> list[list[float]]

Use :func:`make_embedding_backend` to construct the right provider from a
:class:`~pyclaw.config.schema.EmbeddingConfig` object.
"""

import math
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyclaw.config.schema import EmbeddingConfig

logger = logging.getLogger("pyclaw.memory.embeddings")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class EmbeddingBackend(ABC):
    """Async interface for generating text embeddings."""

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Return one embedding vector per text.

        Parameters
        ----------
        texts:
            Non-empty list of strings to embed.

        Returns
        -------
        list of list[float]:
            Parallel list of embedding vectors (same order as *texts*).
        """

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this backend (0 = unknown)."""


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python — no numpy required)
# ---------------------------------------------------------------------------

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Return cosine similarity in [−1, 1] between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

class OpenAIEmbeddingBackend(EmbeddingBackend):
    """OpenAI Embeddings API (``/v1/embeddings``)."""

    _DEFAULT_MODEL = "text-embedding-3-small"
    _DEFAULT_DIMS = 1536

    def __init__(self, config: "EmbeddingConfig") -> None:
        self._model = config.model or self._DEFAULT_MODEL
        self._api_key = config.api_key or None
        self._base_url = config.base_url or None
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        return self._dims or self._DEFAULT_DIMS

    async def embed(self, texts: List[str]) -> List[List[float]]:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package required for OpenAI embeddings: pip install openai"
            ) from exc

        kwargs: dict = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url

        client = AsyncOpenAI(**kwargs)

        req_kwargs: dict = {"input": texts, "model": self._model}
        if self._dims:
            req_kwargs["dimensions"] = self._dims

        resp = await client.embeddings.create(**req_kwargs)
        return [item.embedding for item in resp.data]


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

class GeminiEmbeddingBackend(EmbeddingBackend):
    """Google Gemini Embeddings (``models/text-embedding-004``)."""

    _DEFAULT_MODEL = "models/text-embedding-004"
    _DEFAULT_DIMS = 768

    def __init__(self, config: "EmbeddingConfig") -> None:
        self._model = config.model or self._DEFAULT_MODEL
        self._api_key = config.api_key or None
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        return self._dims or self._DEFAULT_DIMS

    async def embed(self, texts: List[str]) -> List[List[float]]:
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "google-generativeai package required: pip install google-generativeai"
            ) from exc
        import asyncio

        if self._api_key:
            genai.configure(api_key=self._api_key)

        results: List[List[float]] = []
        for text in texts:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda t=text: genai.embed_content(
                    model=self._model,
                    content=t,
                    task_type="retrieval_document",
                ),
            )
            results.append(resp["embedding"])
        return results


# ---------------------------------------------------------------------------
# Local / OpenAI-compatible provider
# ---------------------------------------------------------------------------

class LocalEmbeddingBackend(EmbeddingBackend):
    """
    OpenAI-compatible HTTP embedding server.

    Works with llama.cpp (``--embedding`` flag), Ollama (API-compatible
    mode), and LM Studio.  Defaults to ``http://localhost:11434``.
    """

    _DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, config: "EmbeddingConfig") -> None:
        self._model = config.model or ""
        self._base_url = (config.base_url or self._DEFAULT_BASE_URL).rstrip("/")
        self._api_key = config.api_key or "local"
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        return self._dims

    async def embed(self, texts: List[str]) -> List[List[float]]:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                "httpx package required for local embeddings: pip install httpx"
            ) from exc

        url = f"{self._base_url}/v1/embeddings"
        payload: dict = {"input": texts}
        if self._model:
            payload["model"] = self._model

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data") or data.get("embeddings") or []
        if not items:
            raise RuntimeError(
                f"Local embedding server returned unexpected response: {data}"
            )

        # Support both {embedding: [...]} (OpenAI) and flat [[...]] (some servers)
        if isinstance(items[0], dict):
            return [item["embedding"] for item in items]
        return items  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_embedding_backend(config: "EmbeddingConfig") -> Optional[EmbeddingBackend]:
    """
    Construct an :class:`EmbeddingBackend` from *config*.

    Returns ``None`` if embeddings are disabled (``config.enabled = False``).

    Raises
    ------
    ValueError
        For an unrecognised provider name.
    """
    if not config.enabled:
        return None

    provider = config.provider.lower()
    if provider == "openai":
        return OpenAIEmbeddingBackend(config)
    if provider == "gemini":
        return GeminiEmbeddingBackend(config)
    if provider == "local":
        return LocalEmbeddingBackend(config)

    raise ValueError(
        f"Unknown embedding provider {config.provider!r}. "
        "Supported: openai, gemini, local"
    )
