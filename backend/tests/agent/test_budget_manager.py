"""Phase 11B tests — Token Budget Manager."""

from app.agent.context.budget import BudgetManager, BudgetReport, ContextSize
from app.agent.context.prioritizer import ContextScore, PriorityReport, RankedContextItem
from app.agent.runtime.context import RunContext, WorkingContextItem


def item(content, source="ctx", **metadata):
    return WorkingContextItem(source=source, content=content, metadata=metadata)


def report_of(items):
    """A PriorityReport whose ranked order is the given item order."""
    ranked = [
        RankedContextItem(item=it, score=ContextScore(final_score=float(len(items) - i)), rank=i + 1)
        for i, it in enumerate(items)
    ]
    return PriorityReport(ranked=ranked)


CH8 = "abcdefgh"  # 8 chars -> 2 tokens at chars_per_token=4


# --------------------------------------------------------------------------- #
# Core selection
# --------------------------------------------------------------------------- #

def test_fits_exactly():
    report = report_of([item(CH8), item(CH8)])  # 2 + 2 = 4 tokens
    r = BudgetManager().select(report, budget=4)
    assert [k.content for k in r.kept_items] == [CH8, CH8]
    assert r.used_tokens == 4
    assert r.remaining_tokens == 0
    assert r.dropped_items == []
    assert r.truncated_items == []


def test_overflow_drops_lowest_ranked():
    report = report_of([item(CH8), item(CH8), item(CH8)])  # 2+2+2
    r = BudgetManager().select(report, budget=4)
    assert [k.content for k in r.kept_items] == [CH8, CH8]
    assert len(r.dropped_items) == 1
    assert r.used_tokens == 4


def test_truncation_at_boundary():
    small = item("abcd")            # 1 token
    big = item("x" * 40)            # 10 tokens
    r = BudgetManager().select(report_of([small, big]), budget=6)
    assert r.kept_items[0].content == "abcd"
    assert len(r.truncated_items) == 1
    assert r.kept_items[1].content.endswith("[truncated]")
    assert r.kept_items[1].metadata.get("truncated") is True
    assert r.used_tokens <= 6


def test_zero_budget_keeps_nothing():
    r = BudgetManager().select(report_of([item(CH8), item(CH8)]), budget=0)
    assert r.kept_items == []
    assert r.used_tokens == 0
    assert r.remaining_tokens == 0
    assert len(r.dropped_items) == 2


def test_huge_budget_keeps_everything():
    items = [item(CH8), item("y" * 100), item("z" * 20)]
    r = BudgetManager().select(report_of(items), budget=10_000)
    assert len(r.kept_items) == 3
    assert r.dropped_items == []
    assert r.truncated_items == []


def test_structured_item_never_split():
    big_structured = item("s" * 40, structured=True)  # 10 tokens, not truncatable
    small_text = item("abcd")                          # 1 token
    r = BudgetManager().select(report_of([big_structured, small_text]), budget=5)
    # structured too big -> dropped whole; smaller lower-ranked text still fits
    assert [k.content for k in r.kept_items] == ["abcd"]
    assert big_structured in r.dropped_items
    assert r.truncated_items == []


# --------------------------------------------------------------------------- #
# Planner vs final budgets
# --------------------------------------------------------------------------- #

def test_planner_and_final_budgets_differ():
    report = report_of([item(CH8), item(CH8), item(CH8), item(CH8)])  # 8 tokens total
    bm = BudgetManager()
    planner = bm.select_planner(report, budget=4)   # 2 items
    final = bm.select_final(report, budget=100)      # all 4
    assert len(planner.kept_items) == 2
    assert len(final.kept_items) == 4
    assert len(planner.kept_items) < len(final.kept_items)


def test_ordering_preserved():
    items = [item("a" * 8), item("b" * 8), item("c" * 8)]
    r = BudgetManager().select(report_of(items), budget=10_000)
    assert [k.content for k in r.kept_items] == ["a" * 8, "b" * 8, "c" * 8]


# --------------------------------------------------------------------------- #
# Config / immutability / report shape
# --------------------------------------------------------------------------- #

def test_configurable_chars_per_token():
    # 8-char item = 1 token at cpt=8 (vs 2 at cpt=4)
    r = BudgetManager(chars_per_token=8).select(report_of([item(CH8)]), budget=1)
    assert len(r.kept_items) == 1
    assert r.used_tokens == 1


def test_run_context_not_mutated():
    rc = RunContext.create(
        "q", user_id="u",
        working_context=[item("a" * 40), item("b" * 8)],
    )
    before = [w.content for w in rc.working_context]
    report = report_of(rc.working_context)
    BudgetManager().select(report, budget=3)  # forces truncation of item 0
    after = [w.content for w in rc.working_context]
    assert before == after  # originals untouched (truncation used model_copy)
    assert len(rc.working_context) == 2


def test_budget_report_shape_and_size():
    report = report_of([item(CH8), item(CH8), item(CH8)])
    r = BudgetManager().select(report, budget=4)
    assert isinstance(r, BudgetReport)
    assert isinstance(r.size, ContextSize)
    assert r.size.item_count == len(r.kept_items) == 2
    assert r.size.token_count == r.used_tokens
    assert r.size.char_count == sum(len(k.content) for k in r.kept_items)
