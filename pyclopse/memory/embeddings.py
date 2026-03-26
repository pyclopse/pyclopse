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
:class:`~pyclopse.config.schema.EmbeddingConfig` object.
"""

import math
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyclopse.config.schema import EmbeddingConfig

logger = logging.getLogger("pyclopse.memory.embeddings")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class EmbeddingBackend(ABC):
    """Async interface for generating text embeddings.

    All embedding providers implement this interface.  Callers use
    :func:`make_embedding_backend` to obtain the correct concrete
    implementation based on the ``EmbeddingConfig`` section of the pyclopse
    config file.
    """

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Return one embedding vector per input text.

        Implementors must call the underlying API or model and return the
        resulting floating-point vectors in the same order as *texts*.

        Args:
            texts (List[str]): Non-empty list of strings to embed.

        Returns:
            List[List[float]]: Parallel list of embedding vectors (same
                order as *texts*).
        """

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this backend.

        Implementors should return the actual output dimension, or 0 when
        the dimension is unknown (e.g. determined at runtime by the server).

        Returns:
            int: Vector dimensionality, or 0 if unknown.
        """


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python — no numpy required)
# ---------------------------------------------------------------------------

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Return cosine similarity in [−1, 1] between two floating-point vectors.

    Uses pure Python arithmetic — no numpy dependency.  Returns 0.0 when
    either vector has zero magnitude to avoid division by zero.

    Args:
        a (List[float]): First embedding vector.
        b (List[float]): Second embedding vector (must be the same length
            as *a*).

    Returns:
        float: Cosine similarity score in the range [−1, 1].
    """
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
    """OpenAI Embeddings API provider (``/v1/embeddings``).

    Uses ``text-embedding-3-small`` by default.  A custom ``base_url`` can
    be set to point at any OpenAI-compatible endpoint.

    Attributes:
        _model (str): Embedding model name.
        _api_key (Optional[str]): API key, or None to use environment
            variable ``OPENAI_API_KEY``.
        _base_url (Optional[str]): Override base URL for the API.
        _dims (int): Requested output dimension (0 = use model default).
    """

    _DEFAULT_MODEL = "text-embedding-3-small"
    _DEFAULT_DIMS = 1536

    def __init__(self, config: "EmbeddingConfig") -> None:
        """Initialise the OpenAI embedding backend from config.

        Args:
            config (EmbeddingConfig): Pydantic config object with fields
                ``model``, ``api_key``, ``base_url``, and ``dimensions``.
        """
        self._model = config.model or self._DEFAULT_MODEL
        self._api_key = config.api_key or None
        self._base_url = config.base_url or None
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this backend.

        Returns:
            int: Configured dimension if set, otherwise the model default
                (1536 for ``text-embedding-3-small``).
        """
        return self._dims or self._DEFAULT_DIMS

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed *texts* via the OpenAI Embeddings API.

        Args:
            texts (List[str]): Strings to embed.

        Returns:
            List[List[float]]: One embedding vector per input text.

        Raises:
            RuntimeError: If the ``openai`` package is not installed.
        """
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
    """Google Gemini Embeddings provider (``models/text-embedding-004``).

    Calls the ``genai.embed_content`` function via a thread executor since
    the ``google-generativeai`` SDK is synchronous.

    Attributes:
        _model (str): Gemini embedding model name.
        _api_key (Optional[str]): API key, or None to use the environment.
        _dims (int): Requested output dimension (0 = use model default).
    """

    _DEFAULT_MODEL = "models/text-embedding-004"
    _DEFAULT_DIMS = 768

    def __init__(self, config: "EmbeddingConfig") -> None:
        """Initialise the Gemini embedding backend from config.

        Args:
            config (EmbeddingConfig): Pydantic config object with fields
                ``model``, ``api_key``, and ``dimensions``.
        """
        self._model = config.model or self._DEFAULT_MODEL
        self._api_key = config.api_key or None
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this backend.

        Returns:
            int: Configured dimension if set, otherwise the model default
                (768 for ``text-embedding-004``).
        """
        return self._dims or self._DEFAULT_DIMS

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed *texts* using the Google Gemini Embeddings API.

        Each text is embedded individually via a thread executor because
        the Google SDK is synchronous.

        Args:
            texts (List[str]): Strings to embed.

        Returns:
            List[List[float]]: One embedding vector per input text.

        Raises:
            RuntimeError: If the ``google-generativeai`` package is not
                installed.
        """
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
    """OpenAI-compatible HTTP embedding server provider.

    Works with llama.cpp (``--embedding`` flag), Ollama (API-compatible
    mode), and LM Studio.  Defaults to ``http://localhost:11434``.

    The response body is expected to contain either an OpenAI-style
    ``{"data": [{"embedding": [...]}]}`` envelope or a flat
    ``{"embeddings": [[...]]}`` array.

    Attributes:
        _model (str): Model name to pass to the server (may be empty).
        _base_url (str): Server base URL without trailing slash.
        _api_key (str): Bearer token sent in the ``Authorization`` header.
        _dims (int): Expected dimension (0 = unknown / unrestricted).
    """

    _DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, config: "EmbeddingConfig") -> None:
        """Initialise the local embedding backend from config.

        Args:
            config (EmbeddingConfig): Pydantic config object with fields
                ``model``, ``base_url``, ``api_key``, and ``dimensions``.
        """
        self._model = config.model or ""
        self._base_url = (config.base_url or self._DEFAULT_BASE_URL).rstrip("/")
        self._api_key = config.api_key or "local"
        self._dims = config.dimensions or 0

    @property
    def dimensions(self) -> int:
        """Dimensionality of vectors produced by this backend.

        Returns:
            int: Configured dimension if set, otherwise 0 (unknown).
        """
        return self._dims

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed *texts* by posting to the local OpenAI-compatible server.

        Args:
            texts (List[str]): Strings to embed.

        Returns:
            List[List[float]]: One embedding vector per input text.

        Raises:
            RuntimeError: If the ``httpx`` package is not installed, or if
                the server returns an unexpected response format.
        """
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
    """Construct an :class:`EmbeddingBackend` from *config*.

    Returns ``None`` if embeddings are disabled (``config.enabled = False``).
    Otherwise selects the appropriate provider class based on
    ``config.provider`` and returns an initialised instance.

    Args:
        config (EmbeddingConfig): Pydantic ``EmbeddingConfig`` object read
            from the pyclopse config file.

    Returns:
        Optional[EmbeddingBackend]: A configured embedding backend instance,
            or None if embeddings are disabled.

    Raises:
        ValueError: For an unrecognised provider name (anything other than
            ``"openai"``, ``"gemini"``, or ``"local"``).
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
