"""Deterministic keyword-based capability retriever.

Cheap, stateless (beyond the registry reference), no LLM/embeddings. Filters
the candidate tools, scores them by keyword overlap, and returns the top_k.
Falls back to an evidence-priority ranking when nothing matches.
"""

from app.agent.capabilities.models import (
    CapabilityMatch,
    CapabilityRetrievalRequest,
    CapabilityRetrievalResponse,
)
from app.agent.capabilities.retriever import CapabilityRetriever
from app.agent.capabilities.scoring import score_tool, tokenize
from app.agent.models.tool_spec import ToolSpec
from app.agent.registry.registry import ToolRegistry

_FALLBACK_REASON = "fallback: default ranking by evidence_priority (no keyword match)"


class KeywordCapabilityRetriever(CapabilityRetriever):
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def _candidates(self, request: CapabilityRetrievalRequest) -> list[ToolSpec]:
        tools = (
            self._registry.list_all()
            if request.include_disabled
            else self._registry.list_enabled()
        )

        if request.allowed_kinds is not None:
            allowed = set(request.allowed_kinds)
            tools = [t for t in tools if t.kind in allowed]

        if request.allowed_risk_levels is not None:
            allowed_risk = set(request.allowed_risk_levels)
            tools = [t for t in tools if t.risk_level in allowed_risk]

        if request.required_tags:
            tools = [
                t for t in tools if all(tag in t.tags for tag in request.required_tags)
            ]

        if request.excluded_tool_ids:
            excluded = set(request.excluded_tool_ids)
            tools = [t for t in tools if t.id not in excluded]

        return tools

    def retrieve(
        self, request: CapabilityRetrievalRequest
    ) -> CapabilityRetrievalResponse:
        candidates = self._candidates(request)
        query_tokens = set(tokenize(request.query))

        scored = [(tool, score_tool(query_tokens, tool)) for tool in candidates]
        positive = [(tool, result) for tool, result in scored if result.score > 0]

        if positive:
            # score desc, then id asc for a deterministic tie-break
            positive.sort(key=lambda pair: (-pair[1].score, pair[0].id))
            matches = [
                CapabilityMatch(
                    tool=tool,
                    score=result.score,
                    matched_fields=result.matched_fields,
                    matched_terms=result.matched_terms,
                    reason="keyword match on " + ", ".join(result.matched_fields),
                )
                for tool, result in positive[: request.top_k]
            ]
        else:
            # No keyword signal: rank the same filtered candidates by
            # evidence_priority (desc), then id (asc).
            fallback = sorted(candidates, key=lambda t: (-t.evidence_priority, t.id))
            matches = [
                CapabilityMatch(
                    tool=tool,
                    score=0.0,
                    matched_fields=[],
                    matched_terms=[],
                    reason=_FALLBACK_REASON,
                )
                for tool in fallback[: request.top_k]
            ]

        return CapabilityRetrievalResponse(query=request.query, matches=matches)
