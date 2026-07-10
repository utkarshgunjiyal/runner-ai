"""Token Budget Manager (Phase 11B).

Consumes a PriorityReport (already ranked by the Prioritizer) and selects the
highest-ranked items that fit a token budget, preserving rank order. It never
reranks, never mutates RunContext, and does not import application settings.

Responsibility boundary: Context Engine retrieves, Prioritizer ranks, Budget
Manager selects. Structured items are all-or-nothing (never split); long text
items may be truncated at the budget boundary. Deterministic; no LLM, no
embeddings, no config/database. See backend/app/agent/ARCHITECTURE.md §8.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.agent.context.prioritizer import PriorityReport
from app.agent.runtime.context import WorkingContextItem


class ContextSize(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_count: int
    char_count: int
    token_count: int


class BudgetReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    budget: int
    used_tokens: int
    remaining_tokens: int
    kept_items: list[WorkingContextItem] = Field(default_factory=list)
    truncated_items: list[WorkingContextItem] = Field(default_factory=list)
    dropped_items: list[WorkingContextItem] = Field(default_factory=list)
    size: ContextSize


class BudgetManager:
    DEFAULT_CHARS_PER_TOKEN = 4
    DEFAULT_MIN_TRUNCATE_TOKENS = 5
    STRUCTURED_KEY = "structured"
    _SUFFIX = "\n…[truncated]"

    def __init__(
        self,
        chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
        min_truncate_tokens: int = DEFAULT_MIN_TRUNCATE_TOKENS,
    ) -> None:
        self._cpt = max(1, chars_per_token)
        self._min_truncate = max(1, min_truncate_tokens)

    # -- Public API ----------------------------------------------------------

    def select(self, report: PriorityReport, budget: int) -> BudgetReport:
        """Select ranked items that fit ``budget`` tokens, in rank order."""
        budget = max(0, budget)

        kept: list[WorkingContextItem] = []
        truncated: list[WorkingContextItem] = []
        dropped: list[WorkingContextItem] = []
        used = 0

        ranked = report.ranked
        n = len(ranked)
        index = 0
        while index < n:
            item = ranked[index].item
            cost = self._tokens(item.content)
            remaining = budget - used

            if cost <= remaining:
                kept.append(item)
                used += cost
                index += 1
                continue

            # Does not fully fit.
            if self._is_truncatable(item) and remaining >= self._min_truncate:
                truncated_item = self._truncate(item, remaining)
                kept.append(truncated_item)
                truncated.append(truncated_item)
                used += self._tokens(truncated_item.content)
                index += 1
                break  # budget spent; the rest are dropped

            # Structured (all-or-nothing) or too little budget left: drop and
            # keep scanning — a smaller lower-ranked item may still fit.
            dropped.append(item)
            index += 1

        # Everything after a truncation boundary is dropped.
        dropped.extend(ranked[j].item for j in range(index, n))

        size = ContextSize(
            item_count=len(kept),
            char_count=sum(len(k.content) for k in kept),
            token_count=sum(self._tokens(k.content) for k in kept),
        )
        return BudgetReport(
            budget=budget,
            used_tokens=used,
            remaining_tokens=max(0, budget - used),
            kept_items=kept,
            truncated_items=truncated,
            dropped_items=dropped,
            size=size,
        )

    def select_planner(self, report: PriorityReport, budget: int) -> BudgetReport:
        return self.select(report, budget)

    def select_final(self, report: PriorityReport, budget: int) -> BudgetReport:
        return self.select(report, budget)

    # -- Internals -----------------------------------------------------------

    def _tokens(self, text: str) -> int:
        return (len(text) + self._cpt - 1) // self._cpt  # ceil

    def _is_truncatable(self, item: WorkingContextItem) -> bool:
        # Structured items are all-or-nothing; everything else is text.
        return item.metadata.get(self.STRUCTURED_KEY) is not True

    def _truncate(self, item: WorkingContextItem, remaining_tokens: int) -> WorkingContextItem:
        suffix = self._SUFFIX
        max_chars = remaining_tokens * self._cpt - len(suffix)
        if max_chars < 1:
            max_chars = remaining_tokens * self._cpt
            suffix = ""
        new_content = item.content[:max_chars].rstrip() + suffix
        return item.model_copy(
            update={
                "content": new_content,
                "metadata": {**item.metadata, "truncated": True},
            }
        )
