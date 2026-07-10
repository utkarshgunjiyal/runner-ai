"""Hybrid context retriever (Phase 29).

Runs working-context selection through the Phase 28 ``HybridRetrievalPipeline``:
the deterministic ``ContextPrioritizer`` provides Stage 1 scores, then embedding +
reranker (optional) refine, then top-k + budget. With the default Null stages the
output is identical to today's prioritizer ordering.

Config-free: no LLM SDK, no vector DB, no settings. Never mutates its inputs.
"""

from app.agent.context.budget import BudgetManager
from app.agent.context.prioritizer import ContextPrioritizer
from app.agent.retriever.embedding_retriever import EmbeddingRetriever, NullEmbeddingRetriever
from app.agent.retriever.hybrid_pipeline import (
    Candidate,
    HybridRetrievalPipeline,
    HybridRetrievalResult,
)
from app.agent.retriever.reranker import NullReranker, Reranker
from app.agent.runtime.context import RunContext, WorkingContextItem


class HybridContextRetriever:
    def __init__(
        self,
        *,
        prioritizer: ContextPrioritizer | None = None,
        embedding: EmbeddingRetriever | None = None,
        reranker: Reranker | None = None,
        budget_manager: BudgetManager | None = None,
        pipeline: HybridRetrievalPipeline | None = None,
    ) -> None:
        self._prioritizer = prioritizer or ContextPrioritizer()
        self._pipeline = pipeline or HybridRetrievalPipeline(
            embedding=embedding or NullEmbeddingRetriever(),
            reranker=reranker or NullReranker(),
            budget_manager=budget_manager,
        )

    def retrieve(
        self,
        items: list[WorkingContextItem],
        user_request: str,
        *,
        top_k: int | None = None,
        budget: int | None = None,
    ) -> HybridRetrievalResult:
        # Stage 1: deterministic priority scores over a copy of the items.
        report = self._prioritizer.prioritize(items, user_request)
        candidates = [
            Candidate(
                id=f"ctx-{i}",
                text=ranked.item.content,
                payload=ranked.item,
                deterministic_score=ranked.score.final_score,
                metadata={"source": ranked.item.source},
            )
            for i, ranked in enumerate(report.ranked)
        ]
        return self._pipeline.retrieve(
            user_request,
            candidates,
            top_k=top_k if top_k is not None else len(candidates),
            budget=budget,
        )

    def select_items(
        self,
        items: list[WorkingContextItem],
        user_request: str,
        *,
        top_k: int | None = None,
        budget: int | None = None,
    ) -> list[WorkingContextItem]:
        result = self.retrieve(items, user_request, top_k=top_k, budget=budget)
        return [scored.candidate.payload for scored in result.ranked]

    def select_run_context(self, run_context: RunContext, **kwargs) -> list[WorkingContextItem]:
        # Reads a copy of the working context; never mutates the RunContext.
        return self.select_items(run_context.working_context, run_context.user_request, **kwargs)
