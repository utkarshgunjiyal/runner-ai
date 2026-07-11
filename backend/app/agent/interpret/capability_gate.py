"""Intent-based capability gating (Phase 44). Pure, config-free.

Fixes two routing defects by keeping intent-inappropriate tools out of the
planner's candidate set (the planner never chooses an unavailable tool):

- ``get_page_summary`` is eligible only when a PAGE was explicitly requested;
  broad "summarize this document" / "what is this about?" must not route to a
  page tool.
- ``save_user_preference`` (a write) is eligible only when the user EXPLICITLY
  asks to save a durable preference; casual chat / persistence-test messages
  must never trigger a preference write.

The scope gate computes the excluded ids from the interpretation and stores them
in ``run_context.metadata['excluded_capability_ids']``; a thin retriever wrapper
drops them from RunContext-aware retrieval.
"""

from __future__ import annotations

# Capabilities gated behind an explicit page reference.
_PAGE_TOOLS = frozenset({"get_page_summary"})
# Preference-write capabilities gated behind explicit preference-save language.
_PREFERENCE_WRITE_TOOLS = frozenset({"save_user_preference"})


def disallowed_capability_ids(interpretation) -> set[str]:
    """Deterministically compute the capability ids to exclude for this request."""
    excluded: set[str] = set()
    if not getattr(interpretation, "page_explicit", False):
        excluded |= _PAGE_TOOLS
    if not getattr(interpretation, "preference_write", False):
        excluded |= _PREFERENCE_WRITE_TOOLS
    return excluded


class IntentCapabilityRetriever:
    """Wraps a capability retriever and drops intent-ineligible capabilities from
    RunContext-aware retrieval, reading the excluded-id set the scope gate stored
    in ``run_context.metadata['excluded_capability_ids']``."""

    def __init__(self, base) -> None:
        self._base = base

    def retrieve(self, request):
        return self._base.retrieve(request)

    def retrieve_for_run_context(self, run_context, **kwargs):
        response = self._base.retrieve_for_run_context(run_context, **kwargs)
        excluded = set(getattr(run_context, "metadata", {}).get("excluded_capability_ids") or [])
        if not excluded:
            return response
        kept = [m for m in response.matches if m.tool.id not in excluded]
        return response.model_copy(update={"matches": kept})

    def __getattr__(self, name):
        return getattr(self._base, name)
