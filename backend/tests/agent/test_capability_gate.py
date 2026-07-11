"""Phase 44 — intent-based capability gating (defects 6, 7). Config-free."""

from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.interpret import interpret_request
from app.agent.interpret.capability_gate import (
    IntentCapabilityRetriever,
    disallowed_capability_ids,
)
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec


def _tool(tool_id):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=ToolKind.INTERNAL, description=tool_id,
        input_schema={}, output_schema={}, risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ, requires_approval=False,
    )


def test_page_tool_excluded_without_explicit_page():
    interp = interpret_request("summarize this document", has_thread_documents=True)
    assert "get_page_summary" in disallowed_capability_ids(interp)


def test_page_tool_allowed_with_explicit_page():
    interp = interpret_request("what is on page 3 of the document?", has_thread_documents=True)
    assert "get_page_summary" not in disallowed_capability_ids(interp)


def test_preference_tool_excluded_for_casual_message():
    interp = interpret_request("This is my persistence test message.")
    assert "save_user_preference" in disallowed_capability_ids(interp)


def test_preference_tool_allowed_on_explicit_save():
    for phrase in ("Remember that I prefer concise answers",
                   "From now on, use bullet points",
                   "Save this preference"):
        interp = interpret_request(phrase)
        assert "save_user_preference" not in disallowed_capability_ids(interp), phrase


class _Base:
    def __init__(self, tools):
        self._tools = tools

    def retrieve_for_run_context(self, run_context, **kw):
        return CapabilityRetrievalResponse(
            query="q", matches=[CapabilityMatch(tool=t, score=1.0) for t in self._tools]
        )

    def retrieve(self, request):
        return CapabilityRetrievalResponse(query="q", matches=[])


class _Ctx:
    def __init__(self, excluded):
        self.metadata = {"excluded_capability_ids": excluded}


def test_wrapper_drops_excluded_capabilities():
    wrapper = IntentCapabilityRetriever(
        _Base([_tool("search_documents"), _tool("get_page_summary"), _tool("save_user_preference")])
    )
    ctx = _Ctx(["get_page_summary", "save_user_preference"])
    ids = {m.tool.id for m in wrapper.retrieve_for_run_context(ctx).matches}
    assert ids == {"search_documents"}


def test_wrapper_no_exclusions_is_passthrough():
    wrapper = IntentCapabilityRetriever(_Base([_tool("search_documents")]))

    class _Empty:
        metadata: dict = {}

    assert len(wrapper.retrieve_for_run_context(_Empty()).matches) == 1
