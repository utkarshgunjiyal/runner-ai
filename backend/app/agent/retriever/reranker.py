"""Cross-encoder reranker interface — Stage 3 of hybrid retrieval.

Provider-agnostic: the real implementation later wraps a cross-encoder model;
this package only depends on the interface. A deterministic fake is provided for
tests; a "null" implementation reports itself unavailable so the pipeline skips
reranking and keeps the prior stage's order.

Config-free: stdlib only. No LLM SDKs, no external services, no settings.
"""

from abc import ABC, abstractmethod

from app.agent.retriever.embedding_retriever import tokenize


class Reranker(ABC):
    """Cross-encoder: score each (query, document) pair jointly."""

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def score(self, query: str, texts: list[str]) -> list[float]:
        ...


class FakeReranker(Reranker):
    """Deterministic relevance = shared query-token count (+ phrase bonus).

    Intentionally distinct from the bi-encoder's hashed cosine, so reranking can
    reorder the embedding shortlist in tests.
    """

    def available(self) -> bool:
        return True

    def score(self, query: str, texts: list[str]) -> list[float]:
        query_tokens = set(tokenize(query))
        query_lower = (query or "").lower()
        scores: list[float] = []
        for text in texts:
            overlap = len(query_tokens & set(tokenize(text)))
            phrase_bonus = 0.5 if query_lower and query_lower in (text or "").lower() else 0.0
            scores.append(float(overlap) + phrase_bonus)
        return scores


class NullReranker(Reranker):
    """Always-unavailable stand-in — forces the no-rerank fallback."""

    def available(self) -> bool:
        return False

    def score(self, query: str, texts: list[str]) -> list[float]:
        raise RuntimeError("reranker unavailable")
