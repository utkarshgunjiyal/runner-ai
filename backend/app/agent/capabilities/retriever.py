"""CapabilityRetriever interface.

Implementations rank the registry's tools for a query and return the top
matches. Phase 2 ships one keyword implementation; the interface is stable so
embedding/hybrid/reranker variants slot in later without changing callers
(docs/architecture/v2.md §4).

Phase 15 refinement — RunContext-aware retrieval. Capability selection is no
longer driven by the raw user request alone. ``retrieve_for_run_context``
consumes the whole RunContext (user request + prioritized working context +
behavior profile) and folds those signals into the query before delegating to
the existing ``retrieve``. The original request-based ``retrieve`` API is
preserved unchanged for backward compatibility.
"""

from abc import ABC, abstractmethod

from app.agent.capabilities.models import (
    CapabilityRetrievalRequest,
    CapabilityRetrievalResponse,
)


def build_run_context_query(run_context) -> str:
    """Compose a retrieval query from every useful RunContext signal.

    The raw user request is only one signal; the prioritized working context
    (recent messages, thread summary, preferences, knowledge) and the behavior
    profile's reason are folded in so capability selection reflects the whole
    run, not just the latest sentence. Duck-typed on RunContext to keep this
    module free of runtime imports (and import cycles).
    """

    parts: list[str] = [getattr(run_context, "user_request", "") or ""]

    # Prefer an explicitly prioritized context if the pipeline stored one,
    # otherwise fall back to the working context carried on the RunContext.
    metadata = getattr(run_context, "metadata", None) or {}
    prioritized = metadata.get("prioritized_context")
    items = prioritized if prioritized is not None else getattr(run_context, "working_context", [])
    for item in items or []:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
        if content:
            parts.append(content)

    profile = getattr(run_context, "behavior_profile", None)
    reason = getattr(profile, "reason", "") if profile is not None else ""
    if reason:
        parts.append(reason)

    return " ".join(part for part in parts if part).strip()


def build_run_context_request(
    run_context,
    *,
    query: str | None = None,
    top_k: int = 8,
    include_disabled: bool = False,
    allowed_kinds=None,
    allowed_risk_levels=None,
    required_tags=None,
    excluded_tool_ids=None,
) -> CapabilityRetrievalRequest:
    """Build a CapabilityRetrievalRequest from a RunContext plus optional filters.

    ``query`` overrides the retrieval text. When ``None`` (default) the query is the
    full context-enriched query (``build_run_context_query``). Capability *selection*
    should pass the current request here so conversation history cannot outweigh the
    current intent (Phase 46.2.2); the RunContext is still used for connector/intent
    eligibility filtering by the wrapper retrievers.
    """

    kwargs: dict = {
        "query": query if query is not None else build_run_context_query(run_context),
        "top_k": top_k,
        "include_disabled": include_disabled,
    }
    if allowed_kinds is not None:
        kwargs["allowed_kinds"] = allowed_kinds
    if allowed_risk_levels is not None:
        kwargs["allowed_risk_levels"] = allowed_risk_levels
    if required_tags is not None:
        kwargs["required_tags"] = required_tags
    if excluded_tool_ids is not None:
        kwargs["excluded_tool_ids"] = excluded_tool_ids
    return CapabilityRetrievalRequest(**kwargs)


class CapabilityRetriever(ABC):
    @abstractmethod
    def retrieve(
        self, request: CapabilityRetrievalRequest
    ) -> CapabilityRetrievalResponse:
        ...

    def retrieve_for_run_context(
        self, run_context, **kwargs
    ) -> CapabilityRetrievalResponse:
        """RunContext-aware retrieval (Phase 15).

        Enriches the query with the full RunContext, then delegates to the
        implementation's ``retrieve``. Concrete on the base class so every
        retriever (keyword now, embedding/hybrid later) gains it for free.
        """

        return self.retrieve(build_run_context_request(run_context, **kwargs))
