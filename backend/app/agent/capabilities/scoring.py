"""Deterministic keyword scoring for capability retrieval.

Pure functions, no state, no I/O. Tokenize on non-alphanumeric boundaries,
lowercase, drop tokens shorter than 2 chars, and score a ToolSpec by weighted
term overlap across a fixed set of fields.
"""

import re
from typing import NamedTuple

from app.agent.models.tool_spec import ToolSpec

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 2

# Field → weight. Iteration order is fixed, so matched_fields is deterministic.
FIELD_WEIGHTS: dict[str, int] = {
    "id": 10,
    "name": 10,
    "keywords": 5,
    "tags": 4,
    "capability_tags": 4,
    "typical_user_questions": 4,
    "examples": 3,
    "success_examples": 3,
    "description": 2,
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= _MIN_TOKEN_LEN]


def _field_tokens(value) -> set[str]:
    if isinstance(value, str):
        return set(tokenize(value))
    if isinstance(value, (list, tuple)):
        tokens: set[str] = set()
        for item in value:
            tokens.update(tokenize(str(item)))
        return tokens
    return set()


class ScoreResult(NamedTuple):
    score: float
    matched_fields: list[str]
    matched_terms: list[str]


def score_tool(query_tokens: set[str], tool: ToolSpec) -> ScoreResult:
    """Score one tool against the (already-tokenized) query.

    Each matched term in a field contributes that field's weight; a term that
    matches in several fields reinforces across all of them.
    """
    if not query_tokens:
        return ScoreResult(0.0, [], [])

    total = 0.0
    matched_fields: list[str] = []
    matched_terms: set[str] = set()

    for field, weight in FIELD_WEIGHTS.items():
        overlap = query_tokens & _field_tokens(getattr(tool, field))
        if overlap:
            total += weight * len(overlap)
            matched_fields.append(field)
            matched_terms.update(overlap)

    return ScoreResult(total, matched_fields, sorted(matched_terms))
