"""Embedding (bi-encoder) retrieval interface — Stage 2 of hybrid retrieval.

Provider-agnostic: the real implementation later wraps V1.5's embedding_service
(or any vendor), but this package only depends on the interface. A deterministic
fake is provided for tests; a "null" implementation reports itself unavailable so
the pipeline falls back to the deterministic tier.

Config-free: stdlib only. No LLM SDKs, no external vector DB, no settings.
"""

import hashlib
import math
import re
from abc import ABC, abstractmethod

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DIM = 64  # fake embedding dimensionality


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class EmbeddingRetriever(ABC):
    """Bi-encoder: embed the query and documents into a shared vector space."""

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...


class FakeEmbeddingRetriever(EmbeddingRetriever):
    """Deterministic hashed bag-of-words embedding (no randomness, no network).

    Tokens are hashed (md5, stable across processes) into a fixed-width vector, so
    documents sharing vocabulary with the query score higher under cosine.
    """

    def __init__(self, dim: int = _DIM) -> None:
        self._dim = dim

    def available(self) -> bool:
        return True

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in tokenize(text):
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self._dim
            vec[bucket] += 1.0
        return vec

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]


class NullEmbeddingRetriever(EmbeddingRetriever):
    """Always-unavailable stand-in — forces the deterministic-only fallback."""

    def available(self) -> bool:
        return False

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embedding retriever unavailable")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding retriever unavailable")
