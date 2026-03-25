"""Memory system for pyclawops."""
from .backend import MemoryBackend
from .clawvault import ClawVaultBackend
from .file_backend import FileMemoryBackend
from .service import MemoryService, get_memory_service, set_memory_service
from .embeddings import EmbeddingBackend, make_embedding_backend, cosine_similarity
# Keep legacy import working
from .client import ClawVaultClient

__all__ = [
    "MemoryBackend",
    "ClawVaultBackend",
    "FileMemoryBackend",
    "MemoryService",
    "get_memory_service",
    "set_memory_service",
    "EmbeddingBackend",
    "make_embedding_backend",
    "cosine_similarity",
    "ClawVaultClient",
]
