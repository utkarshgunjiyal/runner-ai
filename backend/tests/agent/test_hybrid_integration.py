"""Phase 29 tests — hybrid retrieval integrated into the three retrieval systems.

Config-free: deterministic fake embedding/reranker, no network, no LLM. Verifies
the Null default reproduces today's deterministic ordering exactly, and that
injected stages narrow/reorder while budgeting is preserved.
"""

import ast
import inspect

from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.capabilities.models import CapabilityRetrievalRequest
from app.agent.context.final_builder import FinalContextBuilder
from app.agent.context.prioritizer import ContextPrioritizer
from app.agent.models.final_prompt import EvidenceSection  # noqa: F401 (kept for parity)
from app.agent.registry.loader import get_default_tool_registry
from app.agent.retriever import capability_retriever as cap_module
from app.agent.retriever import context_retriever as ctx_module
from app.agent.retriever.capability_retriever import HybridCapabilityRetriever
from app.agent.retriever.context_retriever import HybridContextRetriever
from app.agent.retriever.embedding_retriever import FakeEmbeddingRetriever, NullEmbeddingRetriever
from app.agent.retriever.hybrid_pipeline import HybridRetrievalPipeline
from app.agent.retriever.reranker import FakeReranker, NullReranker
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)


# --------------------------------------------------------------------------- #
# Capability retrieval
# --------------------------------------------------------------------------- #

def keyword():
    return KeywordCapabilityRetriever(get_default_tool_registry())


def test_capability_null_matches_keyword_exactly():
    base = keyword()
    hybrid = HybridCapabilityRetriever(keyword())  # Null embedding + reranker
    for query in ("compare page 2 and page 3", "remember I prefer concise answers", "is my job done"):
        req = CapabilityRetrievalRequest(query=query)
        base_ids = [m.tool.id for m in base.retrieve(req).matches]
        hybrid_ids = [m.tool.id for m in hybrid.retrieve(req).matches]
        assert hybrid_ids == base_ids, query


def test_capability_preserves_match_provenance():
    hybrid = HybridCapabilityRetriever(keyword())
    resp = hybrid.retrieve(CapabilityRetrievalRequest(query="summarize page 3"))
    assert resp.matches[0].matched_fields  # original CapabilityMatch preserved
    assert resp.matches[0].tool.id == "get_page_summary"


def test_capability_hybrid_uses_pipeline_and_reranker_reorders():
    # A reranker that inverts order proves the pipeline drives the result.
    class InvertingReranker(FakeReranker):
        def score(self, query, texts):
            return [float(-i) for i in range(len(texts))]  # first candidate scored lowest

    base = keyword()
    req = CapabilityRetrievalRequest(query="summarize page 3")
    base_ids = [m.tool.id for m in base.retrieve(req).matches]
    hybrid = HybridCapabilityRetriever(
        keyword(), embedding=FakeEmbeddingRetriever(), reranker=InvertingReranker())
    hybrid_ids = [m.tool.id for m in hybrid.retrieve(req).matches]
    assert hybrid_ids != base_ids  # reranker changed the ordering


# --------------------------------------------------------------------------- #
# Context retrieval
# --------------------------------------------------------------------------- #

def context_items():
    return [
        WorkingContextItem(source="thread_summary", content="billing and pricing discussion", metadata={"seq": 1}),
        WorkingContextItem(source="recent_message", content="what is the pricing", metadata={"seq": 5}),
        WorkingContextItem(source="user_knowledge", content="unrelated cooking notes"),
    ]


def test_context_null_matches_prioritizer_exactly():
    items = context_items()
    request = "pricing"
    expected = [r.item.content for r in ContextPrioritizer().prioritize(items, request).ranked]
    got = [w.content for w in HybridContextRetriever().select_items(items, request)]
    assert got == expected


def test_context_embedding_narrows_shortlist():
    retriever = HybridContextRetriever(
        embedding=FakeEmbeddingRetriever(),
        pipeline=HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever(), embedding_top_n=1),
    )
    items = context_items()
    selected = retriever.select_items(items, "pricing")
    assert len(selected) == 1


def test_context_budget_respected():
    items = context_items()
    result = HybridContextRetriever().retrieve(items, "pricing", budget=3)
    assert result.used_tokens is not None
    assert result.used_tokens <= 3
    assert "budget" in result.stages_run


def test_context_retriever_does_not_mutate_run_context():
    rc = RunContext.create("pricing", user_id="u", working_context=context_items())
    before = [w.content for w in rc.working_context]
    HybridContextRetriever(embedding=FakeEmbeddingRetriever(), reranker=FakeReranker()).select_run_context(rc)
    assert [w.content for w in rc.working_context] == before


# --------------------------------------------------------------------------- #
# Final context builder
# --------------------------------------------------------------------------- #

def run_context_with_context():
    return RunContext.create(
        "pricing question", user_id="u",
        working_context=[
            WorkingContextItem(source="thread_summary", content="alpha pricing", metadata={"seq": 1}),
            WorkingContextItem(source="recent_message", content="beta note", metadata={"seq": 2}),
        ],
    )


def test_final_builder_null_pipeline_identical_to_default():
    rc = run_context_with_context()
    default_prompt = FinalContextBuilder().build(rc)
    hybrid_prompt = FinalContextBuilder(
        hybrid_pipeline=HybridRetrievalPipeline(
            embedding=NullEmbeddingRetriever(), reranker=NullReranker())
    ).build(rc)
    assert [c.content for c in hybrid_prompt.context_sections] == \
           [c.content for c in default_prompt.context_sections]


def test_final_builder_uses_pipeline_reranker_reorders_context():
    rc = run_context_with_context()

    class InvertingReranker(FakeReranker):
        def score(self, query, texts):
            return [float(-i) for i in range(len(texts))]

    default_order = [c.content for c in FinalContextBuilder().build(rc).context_sections]
    hybrid_order = [
        c.content for c in FinalContextBuilder(
            hybrid_pipeline=HybridRetrievalPipeline(
                embedding=FakeEmbeddingRetriever(), reranker=InvertingReranker())
        ).build(rc).context_sections
    ]
    assert set(hybrid_order) == set(default_order)
    assert hybrid_order != default_order  # reranker changed order


def test_final_builder_budget_still_respected_with_pipeline():
    rc = run_context_with_context()
    rc.append_evidence(EvidenceItem(source="document", content="x" * 40, score=0.9))
    prompt = FinalContextBuilder(
        budget=6,
        hybrid_pipeline=HybridRetrievalPipeline(embedding=FakeEmbeddingRetriever()),
    ).build(rc)
    assert prompt.metadata["tokens_used"] <= 6


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
    for module in (cap_module, ctx_module):
        targets = _module_level_import_targets(module)
        for banned in (
            "app.config", "app.services", "app.db", "motor", "qdrant", "redis",
            "openai", "anthropic", "sentence_transformers", "torch", "genai",
        ):
            assert not any(banned in t for t in targets), (module.__name__, banned, targets)
