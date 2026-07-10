"""Hybrid retrieval pipeline (Phase 28).

The production retrieval architecture locked in ARCHITECTURE.md §26, as a single
generic, provider-agnostic pipeline that all three retrieval systems (Context,
Capability, Final Context) can share:

    deterministic filter/score  (Stage 1 — always runs, authoritative order)
      → embedding retrieval      (Stage 2 — bi-encoder shortlist, optional)
        → cross-encoder rerank   (Stage 3 — reorder, optional)
          → top-k                (Stage 4)
            → budget manager     (Stage 5 — reuses Phase 11B BudgetManager)

Stages 2–3 are injected behind interfaces; when an implementation is absent or
reports itself unavailable, the pipeline degrades gracefully to the deterministic
order (never inverts it). Deterministic and config-free: no LLM SDK, no vector
DB, no settings. Additive — the existing runtime is not rewired to it here.
"""

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.context.budget import BudgetManager
from app.agent.context.prioritizer import ContextScore, PriorityReport, RankedContextItem
from app.agent.retriever.embedding_retriever import EmbeddingRetriever, cosine
from app.agent.retriever.reranker import Reranker
from app.agent.runtime.context import WorkingContextItem


class Candidate(BaseModel):
    """A generic retrieval candidate — a payload plus the text to match on."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str
    text: str
    payload: Any = None
    deterministic_score: float = 0.0
    metadata: dict = Field(default_factory=dict)


class ScoredCandidate(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    candidate: Candidate
    deterministic_score: float
    embedding_score: float | None = None
    rerank_score: float | None = None
    final_score: float
    rank: int


class HybridRetrievalResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    query: str
    ranked: list[ScoredCandidate] = Field(default_factory=list)
    stages_run: list[str] = Field(default_factory=list)
    budget: int | None = None
    used_tokens: int | None = None


class HybridRetrievalPipeline:
    def __init__(
        self,
        *,
        embedding: EmbeddingRetriever | None = None,
        reranker: Reranker | None = None,
        budget_manager: BudgetManager | None = None,
        embedding_top_n: int = 20,
        rerank_top_n: int = 10,
    ) -> None:
        self._embedding = embedding
        self._reranker = reranker
        self._budget_manager = budget_manager or BudgetManager()
        self._embedding_top_n = max(1, embedding_top_n)
        self._rerank_top_n = max(1, rerank_top_n)

    def retrieve(
        self,
        query: str,
        candidates: list[Candidate],
        *,
        top_k: int,
        budget: int | None = None,
    ) -> HybridRetrievalResult:
        stages: list[str] = []

        # -- Stage 1: deterministic filter/score (authoritative baseline order).
        order = sorted(range(len(candidates)), key=lambda i: (-candidates[i].deterministic_score, i))
        work = [candidates[i] for i in order]
        stages.append("deterministic_filter")

        embedding_scores: dict[str, float] = {}
        rerank_scores: dict[str, float] = {}

        # -- Stage 2: embedding retrieval (bi-encoder shortlist).
        if self._stage_available(self._embedding) and work:
            query_vec = self._embedding.embed_query(query)
            doc_vecs = self._embedding.embed_documents([c.text for c in work])
            sims = [cosine(query_vec, dv) for dv in doc_vecs]
            embedding_scores = {work[i].id: sims[i] for i in range(len(work))}
            shortlist = sorted(range(len(work)), key=lambda i: (-sims[i], i))[: self._embedding_top_n]
            work = [work[i] for i in shortlist]
            stages.append("embedding_retrieval")

        # -- Stage 3: cross-encoder rerank.
        if self._stage_available(self._reranker) and work:
            scores = self._reranker.score(query, [c.text for c in work])
            rerank_scores = {work[i].id: scores[i] for i in range(len(work))}
            reordered = sorted(range(len(work)), key=lambda i: (-scores[i], i))[: self._rerank_top_n]
            work = [work[i] for i in reordered]
            stages.append("cross_encoder_rerank")

        # -- Stage 4: top-k.
        work = work[: max(0, top_k)]
        stages.append("top_k")

        scored = [
            ScoredCandidate(
                candidate=c,
                deterministic_score=c.deterministic_score,
                embedding_score=embedding_scores.get(c.id),
                rerank_score=rerank_scores.get(c.id),
                final_score=self._final_score(c, embedding_scores, rerank_scores),
                rank=rank,
            )
            for rank, c in enumerate(work, start=1)
        ]

        # -- Stage 5: budget (reuse the Phase 11B BudgetManager).
        used_tokens = None
        if budget is not None:
            scored, used_tokens = self._apply_budget(scored, budget)
            stages.append("budget")

        return HybridRetrievalResult(
            query=query, ranked=scored, stages_run=stages, budget=budget, used_tokens=used_tokens
        )

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _stage_available(stage) -> bool:
        return stage is not None and stage.available()

    @staticmethod
    def _final_score(candidate, embedding_scores, rerank_scores) -> float:
        if candidate.id in rerank_scores:
            return rerank_scores[candidate.id]
        if candidate.id in embedding_scores:
            return embedding_scores[candidate.id]
        return candidate.deterministic_score

    def _apply_budget(self, scored: list[ScoredCandidate], budget: int):
        report = PriorityReport(
            ranked=[
                RankedContextItem(
                    item=WorkingContextItem(
                        source="hybrid", content=sc.candidate.text, metadata={"_idx": i}
                    ),
                    score=ContextScore(final_score=float(len(scored) - i)),
                    rank=i + 1,
                )
                for i, sc in enumerate(scored)
            ]
        )
        budgeted = self._budget_manager.select(report, budget)
        kept_indices = [it.metadata.get("_idx") for it in budgeted.kept_items]
        kept = [
            scored[i].model_copy(update={"rank": rank})
            for rank, i in enumerate((idx for idx in kept_indices if idx is not None), start=1)
        ]
        return kept, budgeted.used_tokens


# --------------------------------------------------------------------------- #
# Candidate builders for the three retrieval systems
# --------------------------------------------------------------------------- #

def _stable_id(prefix: str, text: str) -> str:
    return f"{prefix}:{hashlib.md5((text or '').encode()).hexdigest()[:12]}"


def candidate_from_context_item(item, deterministic_score: float = 0.0) -> Candidate:
    """Context Retrieval — a WorkingContextItem."""
    return Candidate(
        id=_stable_id("ctx", f"{item.source}:{item.content}"),
        text=item.content,
        payload=item,
        deterministic_score=deterministic_score,
        metadata={"source": item.source},
    )


def candidate_from_capability_match(match) -> Candidate:
    """Capability Retrieval — a CapabilityMatch (ToolSpec + keyword score)."""
    tool = match.tool
    text = " ".join(
        [tool.name, tool.description, *tool.keywords, *tool.capability_tags]
    )
    return Candidate(
        id=tool.id, text=text, payload=tool,
        deterministic_score=float(match.score),
        metadata={"kind": tool.kind.value},
    )


def candidate_from_evidence_section(section) -> Candidate:
    """Final Context Retrieval — an EvidenceSection."""
    return Candidate(
        id=section.id, text=section.content, payload=section,
        deterministic_score=float(section.score) if section.score is not None else 0.0,
        metadata={"source": section.source},
    )
