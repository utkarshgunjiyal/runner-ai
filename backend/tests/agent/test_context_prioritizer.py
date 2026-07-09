"""Phase 11A tests — deterministic Hybrid Context Prioritizer."""

from datetime import datetime

from app.agent.context.prioritizer import (
    ContextPrioritizer,
    ContextScore,
    PriorityReport,
    RankedContextItem,
)
from app.agent.runtime.context import RunContext, WorkingContextItem


def item(source, content="", **metadata):
    return WorkingContextItem(source=source, content=content, metadata=metadata)


def rank(items, request="unrelated words here"):
    return ContextPrioritizer().prioritize(items, request)


# --------------------------------------------------------------------------- #
# Ranking behavior
# --------------------------------------------------------------------------- #

def test_recent_messages_rank_high():
    report = rank([
        item("thread_summary", "a summary"),
        item("recent_message", "hello", seq=5),
        item("user_preference", "dark mode"),
    ])
    assert report.ranked[0].item.source == "recent_message"


def test_pinned_preference_outranks_normal_preference():
    report = rank([
        item("user_preference", "normal one"),
        item("user_preference", "pinned two", pinned=True),
    ])
    assert report.ranked[0].item.content == "pinned two"
    assert "pinned" in report.ranked[0].score.reasons


def test_explicit_reference_boosts_matching_item():
    report = ContextPrioritizer().prioritize(
        [
            item("user_knowledge", "the invoice project deadline"),
            item("user_knowledge", "grocery shopping list"),
        ],
        "tell me about the invoice project",
    )
    top = report.ranked[0]
    assert "invoice" in top.item.content
    assert "explicit_reference" in top.score.reasons
    assert top.score.signals.get("explicit_reference", 0) > 0


def test_active_execution_state_ranks_high():
    report = rank([
        item("recent_message", "hi", seq=10),
        item("active_execution_state", "current plan step running"),
    ])
    assert report.ranked[0].item.source == "active_execution_state"
    assert "execution_relevance" in report.ranked[0].score.reasons


def test_created_at_recency_orders_newer_first():
    report = rank([
        item("user_knowledge", "older", created_at=datetime(2024, 1, 1)),
        item("user_knowledge", "newer", created_at=datetime(2024, 6, 1)),
    ])
    assert report.ranked[0].item.content == "newer"


def test_ties_preserve_original_order():
    report = rank([
        item("user_preference", "a"),
        item("user_preference", "b"),
    ])
    assert [r.item.content for r in report.ranked] == ["a", "b"]
    assert report.ranked[0].score.final_score == report.ranked[1].score.final_score


# --------------------------------------------------------------------------- #
# Edge cases / contract
# --------------------------------------------------------------------------- #

def test_empty_context_returns_empty_report():
    report = ContextPrioritizer().prioritize([], "q")
    assert isinstance(report, PriorityReport)
    assert report.ranked == []
    assert report.is_empty is True


def test_semantic_and_reranker_fields_default_none():
    report = rank([item("recent_message", "hi", seq=1)])
    score = report.ranked[0].score
    assert score.semantic_score is None
    assert score.reranker_score is None
    # deterministic fields are populated
    assert score.final_score > 0
    assert "source_weight" in score.signals


def test_signals_sum_to_final_score():
    report = ContextPrioritizer().prioritize(
        [item("recent_message", "invoice details", seq=3, pinned=True)],
        "the invoice",
    )
    score = report.ranked[0].score
    assert round(sum(score.signals.values()), 4) == score.final_score


# --------------------------------------------------------------------------- #
# RunContext integration
# --------------------------------------------------------------------------- #

def test_run_context_working_context_immutable():
    rc = RunContext.create(
        "invoice project?",
        user_id="u",
        working_context=[item("recent_message", "hi", seq=2), item("user_preference", "p")],
    )
    before = [w.content for w in rc.working_context]
    ContextPrioritizer().prioritize_run_context(rc)
    after = [w.content for w in rc.working_context]
    assert before == after
    assert len(rc.working_context) == 2


def test_metadata_can_store_priority_report():
    rc = RunContext.create(
        "invoice project?",
        user_id="u",
        working_context=[item("user_knowledge", "invoice project notes")],
    )
    ContextPrioritizer().prioritize_run_context(rc, store=True)
    assert "priority_report" in rc.metadata
    stored = rc.metadata["priority_report"]
    assert stored["ranked"][0]["item"]["source"] == "user_knowledge"

    rc2 = RunContext.create("q", user_id="u", working_context=[item("user_preference", "p")])
    ContextPrioritizer().prioritize_run_context(rc2, store=False)
    assert "priority_report" not in rc2.metadata


def test_report_items_in_ranked_order():
    report = rank([
        item("user_preference", "low"),
        item("active_execution_state", "high"),
    ])
    assert [w.content for w in report.items] == ["high", "low"]
