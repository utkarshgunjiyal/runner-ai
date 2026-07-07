"""Embedding provider.

Ships with a deterministic, dependency-free stub so the full ingestion
pipeline (chunk -> embed -> index) works end-to-end without a model server.
A real provider (Phase 2/3) implements the same ``EmbeddingProvider`` interface
and is swapped in via ``get_embedding_provider``.
"""

import hashlib
import math
from typing import Protocol

from app.config import settings


class EmbeddingProvider(Protocol):
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class StubEmbeddingProvider:
    """Hashing embedder: bag-of-tokens hashed into a normalized vector.

    Not semantically meaningful, but stable and unique enough to exercise
    indexing and (later) similarity search wiring deterministically.
    """

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for token in text.lower().split():
            h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
            idx = h % self.dimension
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # Deterministic non-zero fallback for empty/whitespace text.
            seed = int(hashlib.sha256((text or "empty").encode()).hexdigest(), 16)
            vec[seed % self.dimension] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = StubEmbeddingProvider(dimension=settings.embedding_dim)
    return _provider
