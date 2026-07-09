"""Hybrid Context Prioritizer — deterministic tier only (Phase 11A).

Ranks working-context items before token budgeting and planning. This phase
implements deterministic signals only; ``semantic_score`` and ``reranker_score``
fields exist on the score model so the semantic tier and reranker can be added
later without changing the contract.

Responsibility boundary: the Context Engine *retrieves*, this Prioritizer
*ranks*, and the (future) Budget Manager *selects*. This module never mutates
``RunContext.working_context``; it may store a report into ``RunContext.metadata``.
No LLM, no embeddings, no config, no database.
See backend/app/agent/ARCHITECTURE.md §7.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agent.runtime.context import RunContext, WorkingContextItem

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 3
_STOPWORDS = {
    "the", "and", "for", "are", "you", "your", "with", "that", "this", "what",
    "about", "tell", "how", "did", "does", "was", "were", "our", "can", "could",
    "would", "should", "please", "give", "show", "from", "into", "have", "has",
}


def _tokenize(text: str) -> set[str]:
    return {
        t
        for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    }


def _to_float(value) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class ContextScore(BaseModel):
    """A deterministic priority score. Semantic/reranker fields are reserved."""

    model_config = ConfigDict(frozen=True)

    final_score: float
    signals: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    # Reserved for later tiers (Phase 11+); unused in the deterministic tier.
    semantic_score: float | None = None
    reranker_score: float | None = None


class RankedContextItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: WorkingContextItem
    score: ContextScore
    rank: int


class PriorityReport(BaseModel):
    ranked: list[RankedContextItem] = Field(default_factory=list)

    @property
    def items(self) -> list[WorkingContextItem]:
        """Working-context items in ranked (descending) order."""
        return [r.item for r in self.ranked]

    @property
    def is_empty(self) -> bool:
        return not self.ranked


class ContextPrioritizer:
    # Signal weights (contribution = weight * normalized signal value).
    W_SOURCE = 1.0
    W_RECENCY = 0.5
    W_PINNED = 0.6
    W_EXPLICIT = 0.5
    W_EXEC = 1.0

    SOURCE_WEIGHTS = {
        "active_execution_state": 1.0,
        "execution_state": 1.0,
        "recent_message": 0.8,
        "thread_summary": 0.6,
        "user_preference": 0.5,
        "user_knowledge": 0.45,
    }
    DEFAULT_SOURCE_WEIGHT = 0.3
    EXECUTION_SOURCES = {"active_execution_state", "execution_state", "execution"}
    _PINNED_KEYS = ("pinned", "high_priority")

    def __init__(self, source_weights: dict[str, float] | None = None) -> None:
        self._source_weights = source_weights or dict(self.SOURCE_WEIGHTS)

    # -- Public API ----------------------------------------------------------

    def prioritize(
        self, items: list[WorkingContextItem], user_request: str
    ) -> PriorityReport:
        """Pure function: rank items descending by score, stable on ties."""
        items = list(items)
        request_tokens = _tokenize(user_request)

        seqs = [
            m.metadata.get("seq")
            for m in items
            if isinstance(m.metadata.get("seq"), int)
            and not isinstance(m.metadata.get("seq"), bool)
        ]
        max_seq = max(seqs) if seqs else None

        created = [
            f for m in items if (f := _to_float(m.metadata.get("created_at"))) is not None
        ]
        c_min = min(created) if created else None
        c_max = max(created) if created else None

        scored = [
            (index, item, self._score(item, request_tokens, max_seq, c_min, c_max))
            for index, item in enumerate(items)
        ]
        # score desc, original index asc (stable tie-break)
        scored.sort(key=lambda triple: (-triple[2].final_score, triple[0]))

        ranked = [
            RankedContextItem(item=item, score=score, rank=position + 1)
            for position, (_index, item, score) in enumerate(scored)
        ]
        return PriorityReport(ranked=ranked)

    def prioritize_run_context(
        self, run_context: RunContext, store: bool = True
    ) -> PriorityReport:
        """Rank a RunContext's working context. Reads a copy (never mutates it);
        optionally stores the report into ``run_context.metadata``."""
        report = self.prioritize(run_context.working_context, run_context.user_request)
        if store:
            run_context.metadata["priority_report"] = report.model_dump()
        return report

    # -- Scoring -------------------------------------------------------------

    def _recency(self, item, max_seq, c_min, c_max) -> float:
        seq = item.metadata.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and max_seq:
            return seq / max_seq
        created = _to_float(item.metadata.get("created_at"))
        if created is not None and c_max is not None:
            if c_max > c_min:
                return (created - c_min) / (c_max - c_min)
            return 1.0
        return 0.0

    def _is_pinned(self, item) -> bool:
        if any(item.metadata.get(key) is True for key in self._PINNED_KEYS):
            return True
        return item.metadata.get("priority") in {"high", "pinned"}

    def _explicit_value(self, item, request_tokens: set[str]) -> float:
        if not request_tokens:
            return 0.0
        ref_tokens = _tokenize(item.content) | _tokenize(item.source.replace("_", " "))
        matched = request_tokens & ref_tokens
        if not matched:
            return 0.0
        return min(1.0, len(matched) / 3.0)

    def _score(self, item, request_tokens, max_seq, c_min, c_max) -> ContextScore:
        signals: dict[str, float] = {}
        reasons: list[str] = [item.source]

        # 1. source/type weight (always present)
        source_weight = self._source_weights.get(item.source, self.DEFAULT_SOURCE_WEIGHT)
        signals["source_weight"] = round(self.W_SOURCE * source_weight, 4)

        # 5. execution relevance
        if item.source in self.EXECUTION_SOURCES:
            signals["execution_relevance"] = round(self.W_EXEC, 4)
            reasons.append("execution_relevance")

        # 2. recency
        recency = self._recency(item, max_seq, c_min, c_max)
        if recency > 0:
            signals["recency"] = round(self.W_RECENCY * recency, 4)
            reasons.append("recency")

        # 3. pinned / high priority
        if self._is_pinned(item):
            signals["pinned"] = round(self.W_PINNED, 4)
            reasons.append("pinned")

        # 4. explicit reference
        explicit = self._explicit_value(item, request_tokens)
        if explicit > 0:
            signals["explicit_reference"] = round(self.W_EXPLICIT * explicit, 4)
            reasons.append("explicit_reference")

        final_score = round(sum(signals.values()), 4)
        return ContextScore(final_score=final_score, signals=signals, reasons=reasons)
