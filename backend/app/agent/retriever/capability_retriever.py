"""Hybrid capability retriever (Phase 29).

Wraps an existing ``CapabilityRetriever`` (the deterministic keyword tier =
Stage 1) and runs its matches through the Phase 28 ``HybridRetrievalPipeline``
(embedding + reranker, both optional). Implements the same ``CapabilityRetriever``
interface, so it drops into the runtime unchanged and inherits the Phase 15
``retrieve_for_run_context`` for free.

With the default Null embedding + Null reranker the pipeline runs only its
deterministic stage, preserving the wrapped retriever's exact ordering and
returning the original ``CapabilityMatch`` objects (matched fields/score/reason
intact). Config-free: no LLM SDK, no vector DB, no settings.
"""

from app.agent.capabilities.models import (
    CapabilityRetrievalRequest,
    CapabilityRetrievalResponse,
)
from app.agent.capabilities.retriever import CapabilityRetriever
from app.agent.retriever.embedding_retriever import EmbeddingRetriever, NullEmbeddingRetriever
from app.agent.retriever.hybrid_pipeline import Candidate, HybridRetrievalPipeline
from app.agent.retriever.reranker import NullReranker, Reranker


def _capability_text(tool) -> str:
    return " ".join(
        [tool.name, tool.description, *tool.keywords, *tool.capability_tags]
    )


class HybridCapabilityRetriever(CapabilityRetriever):
    def __init__(
        self,
        base: CapabilityRetriever,
        *,
        embedding: EmbeddingRetriever | None = None,
        reranker: Reranker | None = None,
        pipeline: HybridRetrievalPipeline | None = None,
    ) -> None:
        self._base = base
        self._pipeline = pipeline or HybridRetrievalPipeline(
            embedding=embedding or NullEmbeddingRetriever(),
            reranker=reranker or NullReranker(),
        )

    @property
    def base(self) -> CapabilityRetriever:
        return self._base

    def retrieve(self, request: CapabilityRetrievalRequest) -> CapabilityRetrievalResponse:
        # Stage 1: existing deterministic keyword retrieval.
        base_response = self._base.retrieve(request)

        # Candidate conversion (payload keeps the original CapabilityMatch).
        candidates = [
            Candidate(
                id=match.tool.id,
                text=_capability_text(match.tool),
                payload=match,
                deterministic_score=float(match.score),
                metadata={"kind": match.tool.kind.value},
            )
            for match in base_response.matches
        ]

        result = self._pipeline.retrieve(request.query, candidates, top_k=request.top_k)
        matches = [scored.candidate.payload for scored in result.ranked]
        return CapabilityRetrievalResponse(query=request.query, matches=matches)
