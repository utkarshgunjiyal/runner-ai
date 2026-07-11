"""Phase 2 tests — deterministic keyword capability retrieval."""

import pytest
from pydantic import ValidationError

from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.capabilities.models import CapabilityRetrievalRequest
from app.agent.models.tool_spec import RiskLevel, ToolKind
from app.agent.registry.loader import get_default_tool_registry
from app.agent.registry.registry import ToolRegistry


def retriever() -> KeywordCapabilityRetriever:
    return KeywordCapabilityRetriever(get_default_tool_registry())


def ids(response) -> list[str]:
    return [m.tool.id for m in response.matches]


# --------------------------------------------------------------------------- #
# Relevance
# --------------------------------------------------------------------------- #

def test_page_query_returns_page_document_tools_near_top():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="compare page 2 and page 3")
    )
    assert resp.matches, "expected at least one match"
    # get_page_summary is the strongest page/document match
    assert resp.matches[0].tool.id == "get_page_summary"


def test_history_query_returns_conversation_tools_near_top():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="what did we discuss earlier")
    )
    top_two = set(ids(resp)[:2])
    assert top_two == {"get_recent_messages", "get_thread_summary"}


def test_preference_query_returns_preference_tools_near_top():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="remember that I prefer concise answers")
    )
    assert resp.matches[0].tool.id == "save_user_preference"
    assert "get_user_preferences" in ids(resp)[:3]


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def test_disabled_tools_excluded_by_default():
    registry = get_default_tool_registry()
    # rebuild registry with one tool disabled
    disabled = registry.get("get_job_status").model_copy(update={"enabled": False})
    reg2 = ToolRegistry()
    for tool in registry.list_all():
        reg2.register(disabled if tool.id == "get_job_status" else tool)

    resp = KeywordCapabilityRetriever(reg2).retrieve(
        CapabilityRetrievalRequest(query="job status ingestion", top_k=20)
    )
    assert "get_job_status" not in ids(resp)

    resp2 = KeywordCapabilityRetriever(reg2).retrieve(
        CapabilityRetrievalRequest(
            query="job status ingestion", top_k=20, include_disabled=True
        )
    )
    assert "get_job_status" in ids(resp2)


def test_allowed_kinds_filter():
    # No INTERNAL tools when restricted to API → empty result set.
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="search documents", allowed_kinds=[ToolKind.API])
    )
    assert resp.matches == []

    resp2 = retriever().retrieve(
        CapabilityRetrievalRequest(
            query="search documents", allowed_kinds=[ToolKind.INTERNAL]
        )
    )
    assert resp2.matches
    assert all(m.tool.kind == ToolKind.INTERNAL for m in resp2.matches)


def test_allowed_risk_levels_filter():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(
            query="remember prefer", allowed_risk_levels=[RiskLevel.MEDIUM]
        )
    )
    assert all(m.tool.risk_level == RiskLevel.MEDIUM for m in resp.matches)
    assert "save_user_preference" in ids(resp)

    resp2 = retriever().retrieve(
        CapabilityRetrievalRequest(
            query="remember prefer", allowed_risk_levels=[RiskLevel.LOW]
        )
    )
    assert "save_user_preference" not in ids(resp2)


def test_required_tags_filter():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="conversation history", required_tags=["memory"])
    )
    assert resp.matches
    assert all("memory" in m.tool.tags for m in resp.matches)
    assert "search_documents" not in ids(resp)


def test_excluded_tool_ids_filter():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(
            query="remember that I prefer concise answers",
            excluded_tool_ids=["save_user_preference"],
        )
    )
    assert "save_user_preference" not in ids(resp)
    assert "get_user_preferences" in ids(resp)


def test_top_k_respected():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="document page summary memory", top_k=2)
    )
    assert len(resp.matches) <= 2


# --------------------------------------------------------------------------- #
# Determinism & observability
# --------------------------------------------------------------------------- #

def test_results_are_deterministic_for_ties():
    r = retriever()
    req = CapabilityRetrievalRequest(query="conversation history")
    first = ids(r.retrieve(req))
    second = ids(r.retrieve(req))
    assert first == second
    # thread/recent tie → id ascending
    assert first[0] == "get_recent_messages"
    assert first[1] == "get_thread_summary"


def test_matched_fields_and_terms_populated_for_positive_matches():
    resp = retriever().retrieve(
        CapabilityRetrievalRequest(query="remember that I prefer concise answers")
    )
    top = resp.matches[0]
    assert top.tool.id == "save_user_preference"
    assert top.score > 0
    assert top.matched_fields
    assert "remember" in top.matched_terms
    assert "keyword match" in top.reason


def test_fallback_when_no_keyword_matches():
    resp = retriever().retrieve(CapabilityRetrievalRequest(query="zzzz qqqq xyzzy"))
    assert resp.matches
    # highest evidence_priority tool first
    assert resp.matches[0].tool.id == "search_documents"
    assert all(m.score == 0.0 for m in resp.matches)
    assert "fallback" in resp.matches[0].reason


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_empty_query_fails_validation():
    with pytest.raises(ValidationError):
        CapabilityRetrievalRequest(query="")
    with pytest.raises(ValidationError):
        CapabilityRetrievalRequest(query="   ")


def test_invalid_top_k_fails_validation():
    with pytest.raises(ValidationError):
        CapabilityRetrievalRequest(query="hello", top_k=0)
    with pytest.raises(ValidationError):
        CapabilityRetrievalRequest(query="hello", top_k=-3)


# --------------------------------------------------------------------------- #
# RunContext-aware query construction (Phase 46.2.2)
# --------------------------------------------------------------------------- #

class _RC:
    """Minimal duck-typed RunContext for the query builder."""

    def __init__(self, user_request, working_context=None):
        self.user_request = user_request
        self.working_context = working_context or []
        self.metadata = {}
        self.behavior_profile = None


def test_build_run_context_query_folds_working_context():
    from app.agent.capabilities.retriever import build_run_context_query

    rc = _RC("List my repositories", [{"content": "earlier: list open issues"}])
    query = build_run_context_query(rc)
    assert "List my repositories" in query and "issues" in query  # both folded in


def test_query_override_uses_request_only_for_selection():
    # Selection must be driven by the current request, so a query override skips the
    # (potentially topic-polluting) working context entirely.
    from app.agent.capabilities.retriever import build_run_context_request

    rc = _RC("List my repositories", [{"content": "earlier: list open issues and pull requests"}])
    req = build_run_context_request(rc, query=rc.user_request, top_k=5)
    assert req.query == "List my repositories"
    assert "issues" not in req.query
    # Without the override, the working context is folded in (unchanged default).
    default = build_run_context_request(rc, top_k=5)
    assert "issues" in default.query
