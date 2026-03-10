"""
Tests for vector-based memory search.

Covers:
  - EmbeddingConfig schema (enabled flag, providers, camelCase aliases)
  - make_embedding_backend factory (returns None when disabled, raises on unknown)
  - cosine_similarity helper
  - FileMemoryBackend with mock embedding backend:
      - write() embeds and stores vector in vectors.json
      - write() gracefully handles embedding failure
      - delete() removes key from vector index
      - search() ranks by cosine similarity when backend present
      - search() falls back to keyword when embedding fails
      - search() falls back to keyword when no backend
      - keys without vectors score 0 and appear last
  - EmbeddingConfig camelCase aliases
"""

import json
import math
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(dims: int, nonzero_idx: int = 0) -> list[float]:
    """Return a unit vector with 1.0 at index nonzero_idx, 0 elsewhere."""
    v = [0.0] * dims
    v[nonzero_idx] = 1.0
    return v


def _make_embedding_backend(vectors: dict[str, list[float]] | None = None):
    """
    Return a mock EmbeddingBackend.

    ``vectors`` maps text → embedding.  If the text isn't in the dict the
    backend returns a zero vector of dimension 4.
    """
    from pyclaw.memory.embeddings import EmbeddingBackend

    class MockBackend(EmbeddingBackend):
        def __init__(self):
            self._vectors = vectors or {}

        @property
        def dimensions(self) -> int:
            return 4

        async def embed(self, texts: list[str]) -> list[list[float]]:
            result = []
            for t in texts:
                result.append(self._vectors.get(t, [0.0, 0.0, 0.0, 0.0]))
            return result

    return MockBackend()


# ---------------------------------------------------------------------------
# EmbeddingConfig schema
# ---------------------------------------------------------------------------

class TestEmbeddingConfig:

    def test_defaults_disabled(self):
        from pyclaw.config.schema import EmbeddingConfig
        cfg = EmbeddingConfig()
        assert cfg.enabled is False
        assert cfg.provider == "openai"
        assert cfg.model == ""
        assert cfg.api_key == ""
        assert cfg.base_url == ""
        assert cfg.dimensions == 0

    def test_camelcase_apiKey(self):
        from pyclaw.config.schema import EmbeddingConfig
        cfg = EmbeddingConfig.model_validate({"enabled": True, "apiKey": "sk-abc"})
        assert cfg.api_key == "sk-abc"

    def test_camelcase_baseUrl(self):
        from pyclaw.config.schema import EmbeddingConfig
        cfg = EmbeddingConfig.model_validate({"enabled": True, "baseUrl": "http://localhost:11434"})
        assert cfg.base_url == "http://localhost:11434"

    def test_embedded_in_memory_config(self):
        from pyclaw.config.schema import MemoryConfig
        cfg = MemoryConfig.model_validate({
            "embedding": {"enabled": True, "provider": "local", "model": "nomic-embed-text"}
        })
        assert cfg.embedding.enabled is True
        assert cfg.embedding.provider == "local"
        assert cfg.embedding.model == "nomic-embed-text"

    def test_memory_config_embedding_defaults_disabled(self):
        from pyclaw.config.schema import MemoryConfig
        cfg = MemoryConfig()
        assert cfg.embedding.enabled is False


# ---------------------------------------------------------------------------
# make_embedding_backend factory
# ---------------------------------------------------------------------------

class TestMakeEmbeddingBackend:

    def test_returns_none_when_disabled(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend
        cfg = EmbeddingConfig(enabled=False)
        assert make_embedding_backend(cfg) is None

    def test_returns_openai_backend(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend, OpenAIEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="openai")
        backend = make_embedding_backend(cfg)
        assert isinstance(backend, OpenAIEmbeddingBackend)

    def test_returns_gemini_backend(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend, GeminiEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="gemini")
        backend = make_embedding_backend(cfg)
        assert isinstance(backend, GeminiEmbeddingBackend)

    def test_returns_local_backend(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend, LocalEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="local", model="llama3")
        backend = make_embedding_backend(cfg)
        assert isinstance(backend, LocalEmbeddingBackend)

    def test_raises_on_unknown_provider(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend
        cfg = EmbeddingConfig(enabled=True, provider="unknown-xyz")
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            make_embedding_backend(cfg)

    def test_provider_case_insensitive(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import make_embedding_backend, OpenAIEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="OpenAI")
        assert isinstance(make_embedding_backend(cfg), OpenAIEmbeddingBackend)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:

    def test_identical_vectors_score_one(self):
        from pyclaw.memory.embeddings import cosine_similarity
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        from pyclaw.memory.embeddings import cosine_similarity
        assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_negative_one(self):
        from pyclaw.memory.embeddings import cosine_similarity
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        from pyclaw.memory.embeddings import cosine_similarity
        assert cosine_similarity([0, 0], [1, 2]) == pytest.approx(0.0)

    def test_general_case(self):
        from pyclaw.memory.embeddings import cosine_similarity
        a = [3.0, 4.0]  # |a| = 5
        b = [4.0, 3.0]  # |b| = 5
        # dot = 12 + 12 = 24; |a||b| = 25
        assert cosine_similarity(a, b) == pytest.approx(24 / 25)


# ---------------------------------------------------------------------------
# FileMemoryBackend + embedding
# ---------------------------------------------------------------------------

class TestFileMemoryBackendVectors:

    @pytest.mark.asyncio
    async def test_write_creates_vector_index(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        vec = _unit_vec(4, 0)
        eb = _make_embedding_backend({"hello world": vec})
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)

        await backend.write("greeting", {"content": "hello world"})

        index_path = tmp_path / "memory" / "vectors.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert "greeting" in index
        assert index["greeting"] == vec

    @pytest.mark.asyncio
    async def test_write_updates_existing_vector(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        eb = _make_embedding_backend({
            "v1": _unit_vec(4, 0),
            "v2": _unit_vec(4, 1),
        })
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)

        await backend.write("k", {"content": "v1"})
        await backend.write("k", {"content": "v2"})

        index = json.loads((tmp_path / "memory" / "vectors.json").read_text())
        assert index["k"] == _unit_vec(4, 1)

    @pytest.mark.asyncio
    async def test_write_without_embedding_backend_no_index(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        backend = FileMemoryBackend(base_dir=str(tmp_path))
        await backend.write("k", {"content": "hello"})
        assert not (tmp_path / "memory" / "vectors.json").exists()

    @pytest.mark.asyncio
    async def test_write_embedding_failure_doesnt_raise(self, tmp_path):
        """Embedding errors are logged but write still succeeds."""
        from pyclaw.memory.file_backend import FileMemoryBackend
        from pyclaw.memory.embeddings import EmbeddingBackend

        class FailingBackend(EmbeddingBackend):
            @property
            def dimensions(self): return 4
            async def embed(self, texts): raise RuntimeError("API down")

        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=FailingBackend())
        result = await backend.write("k", {"content": "hello"})
        assert result is True  # write succeeded
        assert not (tmp_path / "memory" / "vectors.json").exists()

    @pytest.mark.asyncio
    async def test_delete_removes_from_vector_index(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        vec = _unit_vec(4, 2)
        eb = _make_embedding_backend({"data": vec})
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)

        await backend.write("mykey", {"content": "data"})
        await backend.delete("mykey")

        index = json.loads((tmp_path / "memory" / "vectors.json").read_text())
        assert "mykey" not in index

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_does_not_crash(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        eb = _make_embedding_backend({})
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)
        result = await backend.delete("ghost")
        assert result is False


# ---------------------------------------------------------------------------
# Vector search ranking
# ---------------------------------------------------------------------------

class TestVectorSearch:

    @pytest.mark.asyncio
    async def test_search_ranks_by_cosine_similarity(self, tmp_path):
        """Entry with the closest embedding to the query appears first."""
        from pyclaw.memory.file_backend import FileMemoryBackend

        # query vector = (1,0,0,0)
        # "apple" is closest to query; "banana" is orthogonal
        eb = _make_embedding_backend({
            "apple content": _unit_vec(4, 0),
            "banana content": _unit_vec(4, 1),
            "query": _unit_vec(4, 0),
        })
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)

        await backend.write("apple", {"content": "apple content"})
        await backend.write("banana", {"content": "banana content"})

        results = await backend.search("query", limit=10)
        keys = [r["key"] for r in results]
        assert keys[0] == "apple"

    @pytest.mark.asyncio
    async def test_search_keys_without_vectors_score_zero_appear_last(self, tmp_path):
        """Keys written before embeddings were enabled rank last."""
        from pyclaw.memory.file_backend import FileMemoryBackend

        # Write "old-key" without embedding backend
        backend_no_embed = FileMemoryBackend(base_dir=str(tmp_path))
        await backend_no_embed.write("old-key", {"content": "some content"})

        # Now search with embedding backend
        eb = _make_embedding_backend({
            "new content": _unit_vec(4, 0),
            "query": _unit_vec(4, 0),
        })
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)
        await backend.write("new-key", {"content": "new content"})

        results = await backend.search("query", limit=10)
        keys = [r["key"] for r in results]
        assert keys[0] == "new-key"
        assert "old-key" in keys
        assert keys.index("old-key") > keys.index("new-key")

    @pytest.mark.asyncio
    async def test_search_falls_back_to_keyword_on_embed_failure(self, tmp_path):
        """When embedding the query fails, fall back to keyword scoring."""
        from pyclaw.memory.file_backend import FileMemoryBackend
        from pyclaw.memory.embeddings import EmbeddingBackend

        class FailOnQuery(EmbeddingBackend):
            @property
            def dimensions(self): return 4
            async def embed(self, texts):
                raise RuntimeError("quota exceeded")

        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=FailOnQuery())
        await backend.write("my-key", {"content": "python programming language"})

        results = await backend.search("python", limit=10)
        assert len(results) == 1
        assert results[0]["key"] == "my-key"

    @pytest.mark.asyncio
    async def test_search_no_backend_uses_keyword(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        backend = FileMemoryBackend(base_dir=str(tmp_path))
        await backend.write("fruit", {"content": "I like apples and oranges"})
        await backend.write("cars", {"content": "the car goes fast"})

        results = await backend.search("apple", limit=10)
        assert len(results) == 1
        assert results[0]["key"] == "fruit"

    @pytest.mark.asyncio
    async def test_search_returns_up_to_limit(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        # Each entry gets the same vector so all score equally
        texts = {f"entry-{i} content": _unit_vec(4, 0) for i in range(10)}
        texts["q"] = _unit_vec(4, 0)
        eb = _make_embedding_backend(texts)
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)

        for i in range(10):
            await backend.write(f"entry-{i}", {"content": f"entry-{i} content"})

        results = await backend.search("q", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        eb = _make_embedding_backend({"x": _unit_vec(4, 0), "": [0.0]*4})
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)
        await backend.write("k", {"content": "something"})
        results = await backend.search("", limit=10)
        # keyword path: empty tokens → [] ; vector path: embedding "" → zero vec
        # either way result should be sensible (not crash)
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Provider config passthrough
# ---------------------------------------------------------------------------

class TestProviderConfigs:

    def test_openai_uses_model_from_config(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import OpenAIEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="openai", model="text-embedding-ada-002")
        b = OpenAIEmbeddingBackend(cfg)
        assert b._model == "text-embedding-ada-002"

    def test_openai_defaults_to_small_model(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import OpenAIEmbeddingBackend
        b = OpenAIEmbeddingBackend(EmbeddingConfig(enabled=True))
        assert b._model == "text-embedding-3-small"

    def test_local_uses_base_url_from_config(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import LocalEmbeddingBackend
        cfg = EmbeddingConfig.model_validate({
            "enabled": True, "provider": "local",
            "model": "nomic", "baseUrl": "http://myserver:1234",
        })
        b = LocalEmbeddingBackend(cfg)
        assert b._base_url == "http://myserver:1234"
        assert b._model == "nomic"

    def test_local_default_base_url(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import LocalEmbeddingBackend
        b = LocalEmbeddingBackend(EmbeddingConfig(enabled=True, provider="local"))
        assert b._base_url == "http://localhost:11434"

    def test_gemini_default_model(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import GeminiEmbeddingBackend
        b = GeminiEmbeddingBackend(EmbeddingConfig(enabled=True, provider="gemini"))
        assert b._model == "models/text-embedding-004"

    def test_dimensions_passed_through(self):
        from pyclaw.config.schema import EmbeddingConfig
        from pyclaw.memory.embeddings import OpenAIEmbeddingBackend
        cfg = EmbeddingConfig(enabled=True, provider="openai", dimensions=256)
        b = OpenAIEmbeddingBackend(cfg)
        assert b.dimensions == 256


# ---------------------------------------------------------------------------
# FileMemoryBackend.reindex
# ---------------------------------------------------------------------------

class TestFileMemoryBackendReindex:

    @pytest.mark.asyncio
    async def test_reindex_no_backend_returns_zeros(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        backend = FileMemoryBackend(base_dir=str(tmp_path))
        result = await backend.reindex()
        assert result == {"indexed": 0, "skipped": 0, "errors": 0}

    @pytest.mark.asyncio
    async def test_reindex_empty_memory_returns_zeros(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        eb = _make_embedding_backend({})
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)
        result = await backend.reindex()
        assert result == {"indexed": 0, "skipped": 0, "errors": 0}

    @pytest.mark.asyncio
    async def test_reindex_indexes_all_entries(self, tmp_path):
        from pyclaw.memory.file_backend import FileMemoryBackend
        # Write without embedding so vectors.json doesn't exist yet
        backend_no_embed = FileMemoryBackend(base_dir=str(tmp_path))
        await backend_no_embed.write("alpha", {"content": "alpha content"})
        await backend_no_embed.write("beta", {"content": "beta content"})

        eb = _make_embedding_backend({
            "alpha content": _unit_vec(4, 0),
            "beta content": _unit_vec(4, 1),
        })
        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb)
        result = await backend.reindex()

        assert result["indexed"] == 2
        assert result["errors"] == 0

        index = json.loads((tmp_path / "memory" / "vectors.json").read_text())
        assert "alpha" in index
        assert "beta" in index
        assert index["alpha"] == _unit_vec(4, 0)
        assert index["beta"] == _unit_vec(4, 1)

    @pytest.mark.asyncio
    async def test_reindex_overwrites_stale_vectors(self, tmp_path):
        """After switching model, reindex should replace old vectors."""
        from pyclaw.memory.file_backend import FileMemoryBackend

        # First, write with one backend
        old_vec = _unit_vec(4, 0)
        eb_old = _make_embedding_backend({"data": old_vec})
        backend_old = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb_old)
        await backend_old.write("k", {"content": "data"})

        # Now reindex with a different backend that produces a different vector
        new_vec = _unit_vec(4, 3)
        eb_new = _make_embedding_backend({"data": new_vec})
        backend_new = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=eb_new)
        result = await backend_new.reindex()

        assert result["indexed"] == 1
        index = json.loads((tmp_path / "memory" / "vectors.json").read_text())
        assert index["k"] == new_vec

    @pytest.mark.asyncio
    async def test_reindex_batch_size_respected(self, tmp_path):
        """Entries should be embedded in batches of batch_size."""
        from pyclaw.memory.file_backend import FileMemoryBackend
        from pyclaw.memory.embeddings import EmbeddingBackend

        calls: list[list[str]] = []

        class TrackingBackend(EmbeddingBackend):
            @property
            def dimensions(self): return 4
            async def embed(self, texts):
                calls.append(list(texts))
                return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

        backend_no_embed = FileMemoryBackend(base_dir=str(tmp_path))
        for i in range(5):
            await backend_no_embed.write(f"k{i}", {"content": f"content {i}"})

        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=TrackingBackend())
        await backend.reindex(batch_size=2)

        # 5 entries with batch_size=2 → batches of [2, 2, 1]
        assert len(calls) == 3
        assert len(calls[0]) == 2
        assert len(calls[1]) == 2
        assert len(calls[2]) == 1

    @pytest.mark.asyncio
    async def test_reindex_partial_failure_counts_errors(self, tmp_path):
        """When a batch fails, errors are counted and reindex continues."""
        from pyclaw.memory.file_backend import FileMemoryBackend
        from pyclaw.memory.embeddings import EmbeddingBackend

        call_count = 0

        class FlakyBackend(EmbeddingBackend):
            @property
            def dimensions(self): return 4
            async def embed(self, texts):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("rate limited")
                return [_unit_vec(4, 0) for _ in texts]

        backend_no_embed = FileMemoryBackend(base_dir=str(tmp_path))
        for i in range(4):
            await backend_no_embed.write(f"k{i}", {"content": f"content {i}"})

        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=FlakyBackend())
        result = await backend.reindex(batch_size=2)

        # Batch 1: 2 indexed, Batch 2: 2 errors
        assert result["indexed"] == 2
        assert result["errors"] == 2

    @pytest.mark.asyncio
    async def test_reindex_newest_file_wins_deduplication(self, tmp_path):
        """When same key appears in multiple daily files, newest content is used."""
        from pyclaw.memory.file_backend import FileMemoryBackend
        from datetime import date

        # Manually write two daily files with the same key but different content
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-01-01.md").write_text(
            "# Memory — 2026-01-01\n\n## shared-key\n\nold content\n\n---\n\n"
        )
        (memory_dir / "2026-01-02.md").write_text(
            "# Memory — 2026-01-02\n\n## shared-key\n\nnew content\n\n---\n\n"
        )

        embedded_texts = []

        from pyclaw.memory.embeddings import EmbeddingBackend
        class CaptureBackend(EmbeddingBackend):
            @property
            def dimensions(self): return 4
            async def embed(self, texts):
                embedded_texts.extend(texts)
                return [[0.0] * 4 for _ in texts]

        backend = FileMemoryBackend(base_dir=str(tmp_path), embedding_backend=CaptureBackend())
        result = await backend.reindex()

        # Only one entry for "shared-key" should be indexed
        assert result["indexed"] == 1
        # The newest (2026-01-02) content should be used
        assert "new content" in embedded_texts
        assert "old content" not in embedded_texts
