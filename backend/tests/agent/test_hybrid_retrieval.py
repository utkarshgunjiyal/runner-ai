"""Phase 28 tests — hybrid retrieval pipeline (deterministic → embedding →
rerank → top-k → budget).

Config-free: deterministic fake embedding/reranker, no network, no vector DB, no
LLM. The pipeline is generic and exercised over all three candidate builders.
"""

import ast
import inspect

from app.agent.capabilities.models import CapabilityMatch
from app.agent.models.final_prompt import EvidenceSection
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.retriever import hybrid_pipeline as pipeline_module
from app.agent.retriever.embedding_retriever import (
    FakeEmbeddingRetriever,
    NullEmbeddingRetriever,
)
from app.agent.retriever.hybrid_pipeline import (
    Candidate,
    HybridRetrievalPipeline,
    candidate_from_capability_match,
    candidate_from_context_item,
    candidate_from_evidence_section,
)
from app.agent.retriever.reranker import FakeReranker, NullReranker
from app.agent.runtime.context import WorkingContextItem


def cand(cid, text, det=0.0):
    return Candidate(id=cid, text=text, deterministic_score=det)


CANDIDATES = [
    cand("a", "pricing plans and billing details", det=0.1),
    cand("b", "refund policy and returns", det=0.9),
    cand("c", "the monthly pricing tier for billing", det=0.2),
    cand("d", "unrelated cooking recipes", det=0.0),
]


# --------------------------------------------------------------------------- #
# Stage ordering + gating
# --------------------------------------------------------------------------- #

def test_deterministic_filter_runs_first_and_alone():
    pipe = HybridRetrievalPipeline()  # no embedding, no reranker
    result = pipe.retrieve("pricing", CANDIDATES, top_k=4)
    assert result.stages_run == ["deterministic_filter", "top_k"]
    # ordered by deterministic_score desc
    assert [s.candidate.id for s in result.ranked] == ["b", "c", "a", "d"]


def test_embedding_stage_narrows_candidates():
    pipe = HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever(), embedding_top_n=2)
    result = pipe.retrieve("pricing billing", CANDIDATES, top_k=10)
    assert "embedding_retrieval" in result.stages_run
    assert len(result.ranked) <= 2  # shortlisted by the bi-encoder
    # the semantically-matching candidates should survive
    ids = {s.candidate.id for s in result.ranked}
    assert ids <= {"a", "c"}
    assert all(s.embedding_score is not None for s in result.ranked)


def test_reranker_reorders_candidates():
    # "c" ("the monthly pricing tier for billing") shares 4 query tokens vs "a"'s
    # 2, so the cross-encoder promotes it above the deterministic winner "b".
    pipe = HybridRetrievalPipeline(
        embedding=FakeEmbeddingRetriever(), reranker=FakeReranker(),
        embedding_top_n=4, rerank_top_n=4,
    )
    result = pipe.retrieve("monthly pricing tier billing", CANDIDATES, top_k=3)
    assert "cross_encoder_rerank" in result.stages_run
    assert result.ranked[0].candidate.id == "c"  # most query-token overlap
    assert result.ranked[0].rerank_score >= result.ranked[-1].rerank_score


def test_budget_manager_still_applies():
    pipe = HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever(), reranker=FakeReranker())
    result = pipe.retrieve("pricing billing", CANDIDATES, top_k=4, budget=4)
    assert "budget" in result.stages_run
    assert result.used_tokens is not None
    assert result.used_tokens <= 4
    # ranks are renumbered after budgeting
    assert [s.rank for s in result.ranked] == list(range(1, len(result.ranked) + 1))


# --------------------------------------------------------------------------- #
# Graceful fallback
# --------------------------------------------------------------------------- #

def test_fallback_when_embedding_unavailable():
    pipe = HybridRetrievalPipeline(embedding=NullEmbeddingRetriever(), reranker=FakeReranker())
    result = pipe.retrieve("pricing", CANDIDATES, top_k=4)
    assert "embedding_retrieval" not in result.stages_run
    assert "cross_encoder_rerank" in result.stages_run
    # all candidates still considered (no embedding shortlist)
    assert len(result.ranked) == 4


def test_fallback_when_reranker_unavailable():
    pipe = HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever(), reranker=NullReranker())
    result = pipe.retrieve("pricing", CANDIDATES, top_k=4)
    assert "cross_encoder_rerank" not in result.stages_run
    assert "embedding_retrieval" in result.stages_run


def test_fully_deterministic_fallback():
    pipe = HybridRetrievalPipeline(embedding=NullEmbeddingRetriever(), reranker=NullReranker())
    result = pipe.retrieve("pricing", CANDIDATES, top_k=2)
    assert result.stages_run == ["deterministic_filter", "top_k"]
    assert [s.candidate.id for s in result.ranked] == ["b", "c"]  # det order, top-2


# --------------------------------------------------------------------------- #
# Applies to all three retrieval systems
# --------------------------------------------------------------------------- #

def make_capability(tool_id, keywords):
    tool = ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=f"{tool_id} capability",
        keywords=keywords, input_schema={}, output_schema={},
        risk_level=RiskLevel.LOW, side_effects=SideEffectType.READ, requires_approval=False,
    )
    return CapabilityMatch(tool=tool, score=1.0)


def test_pipeline_over_capability_candidates():
    matches = [
        make_capability("search_documents", ["search", "documents", "pricing"]),
        make_capability("get_job_status", ["job", "status"]),
    ]
    candidates = [candidate_from_capability_match(m) for m in matches]
    pipe = HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever(), reranker=FakeReranker())
    result = pipe.retrieve("find pricing in documents", candidates, top_k=2)
    assert result.ranked[0].candidate.id == "search_documents"
    assert result.ranked[0].candidate.payload.kind == ToolKind.INTERNAL


def test_pipeline_over_context_and_evidence_candidates():
    ctx = [
        candidate_from_context_item(WorkingContextItem(source="thread_summary", content="billing pricing history"), 0.5),
        candidate_from_context_item(WorkingContextItem(source="recent_message", content="unrelated note"), 0.4),
    ]
    ev = candidate_from_evidence_section(EvidenceSection(id="E1", source="document", content="pricing text", score=0.8))
    pipe = HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever())
    ctx_result = pipe.retrieve("pricing", ctx, top_k=2)
    assert ctx_result.ranked[0].candidate.metadata["source"] == "thread_summary"
    ev_result = pipe.retrieve("pricing", [ev], top_k=1)
    assert ev_result.ranked[0].candidate.id == "E1"


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #

def _module_level_import_targets(module):
    tree = ast.parse(inspect.getsource(module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_db_or_vendor_imports():
    import app.agent.retriever.embedding_retriever as emb
    import app.agent.retriever.reranker as rr
    for module in (pipeline_module, emb, rr):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "qdrant", "redis",
            "openai", "anthropic", "sentence_transformers", "torch", "genai",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
