"""Phase 14 tests — Direct Runtime (the non-planning execution path).

Config-free: capability retrieval runs against an in-memory ToolRegistry, and
execution goes through injected fake executors that return AdapterResults. No
Mongo/Qdrant/Redis, no application settings, no LLM. Async ``run`` is driven via
``asyncio.run`` (no pytest-asyncio dependency).
"""

import ast
import asyncio
import inspect

import pytest

from app.agent.capabilities.keyword_retriever import KeywordCapabilityRetriever
from app.agent.capabilities.models import (
    CapabilityMatch,
    CapabilityRetrievalRequest,
    CapabilityRetrievalResponse,
)
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.registry.loader import get_default_tool_registry
from app.agent.runtime import direct_runtime as direct_module
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    WorkingContextItem,
)
from app.agent.runtime.direct_runtime import (
    DirectRuntime,
    ExecutionStatus,
    NotDirectPathError,
    RecoveryStrategy,
)
from app.agent.tools.result import AdapterResult, ErrorCode


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

def make_tool(tool_id: str) -> ToolSpec:
    return ToolSpec(
        id=tool_id,
        name=tool_id,
        kind=ToolKind.INTERNAL,
        description=f"{tool_id} tool",
        input_schema={},
        output_schema={},
        risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ,
        requires_approval=False,
    )


class FakeRetriever:
    """Returns a fixed, ordered match list — deterministic primary/fallback."""

    def __init__(self, tools):
        self._tools = tools

    def retrieve(self, request):
        matches = [
            CapabilityMatch(tool=tool, score=float(len(self._tools) - i))
            for i, tool in enumerate(self._tools)
        ]
        return CapabilityRetrievalResponse(query=request.query, matches=matches)


class FakeExecutor:
    """Maps tool.id → an iterator of AdapterResults; records calls."""

    def __init__(self, scripts):
        # scripts: {tool_id: [AdapterResult, ...]}
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []  # list of (tool_id, args)

    async def execute(self, tool: ToolSpec, args: dict) -> AdapterResult:
        self.calls.append((tool.id, args))
        queue = self._scripts.get(tool.id)
        if not queue:
            return AdapterResult.failure(ErrorCode.NOT_FOUND)
        return queue.pop(0) if len(queue) > 1 else queue[0]


def direct_context(request="What is my job status?", **kw):
    rc = RunContext.create(request, user_id="u", **kw)
    rc.attach_behavior_profile(
        BehaviorProfile(path=BehaviorPath.DIRECT, reason="test", confidence=0.9)
    )
    return rc


EVIDENCE = [EvidenceItem(source="document", content="hit", score=0.9)]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_direct_request_executes():
    tool = make_tool("get_job_status")
    executor = FakeExecutor({"get_job_status": [AdapterResult.ok(output={"status": "done"})]})
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(direct_context()))

    assert rc.metadata["execution_status"] == ExecutionStatus.SUCCESS.value
    assert len(rc.tool_outputs) == 1
    assert rc.tool_outputs[0].capability_id == "get_job_status"
    assert rc.tool_outputs[0].output == {"status": "done"}


def test_planner_path_rejected():
    rc = RunContext.create("Send an email and then schedule a call", user_id="u")
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi"))
    with pytest.raises(NotDirectPathError):
        run(DirectRuntime(FakeRetriever([make_tool("x")]), FakeExecutor({})).run(rc))


def test_capability_selected_uses_real_retrieval():
    # Use the real KeywordCapabilityRetriever + default registry.
    retriever = KeywordCapabilityRetriever(get_default_tool_registry())
    expected = retriever.retrieve(
        CapabilityRetrievalRequest(query="Is my document done processing?")
    ).matches[0].tool.id

    executor = FakeExecutor({expected: [AdapterResult.ok(output={"ok": True})]})
    rc = run(DirectRuntime(retriever, executor).run(
        direct_context("Is my document done processing?")))

    assert rc.selected_capabilities == [expected]
    assert executor.calls[0][0] == expected


def test_adapter_invoked_with_built_args():
    tool = make_tool("get_thread_summary")
    executor = FakeExecutor({"get_thread_summary": [AdapterResult.ok(output={"summary": "s"})]})
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(
        direct_context("what have we discussed", thread_id="t1")))

    assert len(executor.calls) == 1
    _, args = executor.calls[0]
    assert args["query"] == "what have we discussed"
    assert args["user_id"] == "u"
    assert args["thread_id"] == "t1"


def test_evidence_appended():
    tool = make_tool("search_documents")
    executor = FakeExecutor(
        {"search_documents": [AdapterResult.ok(output={"hits": [1]}, evidence=EVIDENCE)]}
    )
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(direct_context("find pricing")))
    assert len(rc.evidence) == 1
    assert rc.evidence[0].content == "hit"


def test_tool_output_metadata_recorded():
    tool = make_tool("get_job_status")
    executor = FakeExecutor(
        {"get_job_status": [AdapterResult.ok(output={"status": "done"}, confidence=0.8)]}
    )
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(direct_context()))
    meta = rc.tool_outputs[0].metadata
    assert meta["success"] is True
    assert meta["confidence"] == 0.8
    assert rc.metadata["direct_runtime"]["capability_id"] == "get_job_status"


# --------------------------------------------------------------------------- #
# Deterministic recovery (no reflection)
# --------------------------------------------------------------------------- #

def test_retryable_error_handled():
    tool = make_tool("get_job_status")
    executor = FakeExecutor(
        {
            "get_job_status": [
                AdapterResult.failure(ErrorCode.UPSTREAM_TIMEOUT, retryable=True),
                AdapterResult.ok(output={"status": "done"}),
            ]
        }
    )
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(direct_context()))

    assert rc.metadata["execution_status"] == ExecutionStatus.SUCCESS.value
    assert len(executor.calls) == 2  # original + one retry
    strategies = [e["strategy"] for e in rc.metadata["recovery_events"]]
    assert RecoveryStrategy.RETRY.value in strategies


def test_fallback_capability_executed():
    primary = make_tool("primary_cap")
    fallback = make_tool("fallback_cap")
    executor = FakeExecutor(
        {
            "primary_cap": [AdapterResult.failure(ErrorCode.UPSTREAM_ERROR, retryable=False)],
            "fallback_cap": [AdapterResult.ok(output={"ok": True})],
        }
    )
    rc = run(DirectRuntime(FakeRetriever([primary, fallback]), executor).run(direct_context()))

    assert rc.selected_capabilities == ["fallback_cap"]
    assert rc.metadata["execution_status"] == ExecutionStatus.SUCCESS.value
    assert [c[0] for c in executor.calls] == ["primary_cap", "fallback_cap"]
    strategies = [e["strategy"] for e in rc.metadata["recovery_events"]]
    assert RecoveryStrategy.FALLBACK.value in strategies


def test_partial_result_propagated():
    tool = make_tool("get_thread_summary")
    executor = FakeExecutor(
        {"get_thread_summary": [AdapterResult.ok(output={"summary": "x"}, partial=True)]}
    )
    rc = run(DirectRuntime(FakeRetriever([tool]), executor).run(direct_context()))

    assert rc.metadata["execution_status"] == ExecutionStatus.PARTIAL.value
    assert rc.metadata["direct_runtime"]["partial"] is True
    assert len(rc.tool_outputs) == 1  # partial output still propagated
    strategies = [e["strategy"] for e in rc.metadata["recovery_events"]]
    assert RecoveryStrategy.PARTIAL.value in strategies


def test_all_failures_ask_user():
    primary = make_tool("p")
    fallback = make_tool("f")
    executor = FakeExecutor(
        {
            "p": [AdapterResult.failure(ErrorCode.UPSTREAM_ERROR)],
            "f": [AdapterResult.failure(ErrorCode.UPSTREAM_ERROR)],
        }
    )
    rc = run(DirectRuntime(FakeRetriever([primary, fallback]), executor).run(direct_context()))
    assert rc.metadata["execution_status"] == ExecutionStatus.NEEDS_USER.value
    strategies = [e["strategy"] for e in rc.metadata["recovery_events"]]
    assert RecoveryStrategy.ASK_USER.value in strategies


def test_no_capability_found_asks_user():
    executor = FakeExecutor({})
    rc = run(DirectRuntime(FakeRetriever([]), executor).run(direct_context()))
    assert rc.metadata["execution_status"] == ExecutionStatus.NEEDS_USER.value
    assert rc.tool_outputs == []
    assert executor.calls == []


def test_only_one_capability_on_happy_path():
    tool = make_tool("get_job_status")
    executor = FakeExecutor({"get_job_status": [AdapterResult.ok(output={"status": "done"})]})
    run(DirectRuntime(FakeRetriever([tool, make_tool("other")]), executor).run(direct_context()))
    # Success on the primary → the fallback capability is never executed.
    assert [c[0] for c in executor.calls] == ["get_job_status"]


# --------------------------------------------------------------------------- #
# Immutability + isolation guarantees
# --------------------------------------------------------------------------- #

def test_working_context_preserved():
    wc = [WorkingContextItem(source="thread_summary", content="prior")]
    rc = direct_context(working_context=wc)
    before = [w.content for w in rc.working_context]

    tool = make_tool("get_job_status")
    executor = FakeExecutor({"get_job_status": [AdapterResult.ok(output={"status": "done"})]})
    run(DirectRuntime(FakeRetriever([tool]), executor).run(rc))

    assert [w.content for w in rc.working_context] == before
    assert len(rc.working_context) == 1


def _module_level_import_targets(module):
    tree = ast.parse(inspect.getsource(module))
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
    return targets


def test_no_config_db_or_llm_imports():
    targets = _module_level_import_targets(direct_module)
    for banned in (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant", "llm",
    ):
        assert not any(banned in t for t in targets), (banned, targets)
    # No LLM/provider invocation in the code (imports checked above; guard calls).
    src = inspect.getsource(direct_module).lower()
    assert "llm_provider" not in src
    assert "llm_client" not in src
