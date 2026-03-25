"""Memory system for pyclawops."""
from .backend import MemoryBackend
from .file_backend import FileMemoryBackend
from .service import MemoryService, get_memory_service, set_memory_service
from .embeddings import EmbeddingBackend, make_embedding_backend, cosine_similarity

__all__ = [
    "MemoryBackend",
    "FileMemoryBackend",
    "MemoryService",
    "get_memory_service",
    "set_memory_service",
    "EmbeddingBackend",
    "make_embedding_backend",
    "cosine_similarity",
]
