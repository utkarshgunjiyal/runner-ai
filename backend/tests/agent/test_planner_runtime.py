"""Phase 15 tests — Planner Runtime (sequential orchestration over DirectRuntime)
and RunContext-aware Capability Retrieval.

Config-free: capability retrieval runs against an in-memory registry (or a fake),
and execution goes through a controllable DirectRuntime stand-in. No
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
from app.agent.models.execution import StepStatus
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec
from app.agent.registry.loader import get_default_tool_registry
from app.agent.runtime import planner_runtime as planner_module
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)
from app.agent.runtime.direct_runtime import ExecutionStatus as DirectStatus
from app.agent.runtime.planner_runtime import (
    ExecutionPlan,
    NotPlannerPathError,
    PlannerRuntime,
    PlannerTask,
    RuntimeStatus,
)


def run(coro):
    return asyncio.run(coro)


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


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

class FakeDirectRuntime:
    """Stand-in for the only execution engine. Consumes scripted outcomes in
    order and mutates the task RunContext exactly as DirectRuntime would."""

    def __init__(self, outcomes):
        # outcomes: list of dicts {status, capability?, output?, evidence?,
        #                          recovery?, policy_block?, requires_approval?}
        self._outcomes = list(outcomes)
        self.calls = []  # (user_request, working_context_len)

    async def run(self, run_context: RunContext) -> RunContext:
        self.calls.append((run_context.user_request, len(run_context.working_context)))
        o = self._outcomes.pop(0)
        run_context.metadata["execution_status"] = o["status"]
        if o.get("capability"):
            run_context.attach_selected_capabilities([o["capability"]])
            run_context.append_tool_output(
                ToolOutput(capability_id=o["capability"], output=o.get("output", {}))
            )
        for ev in o.get("evidence", []):
            run_context.append_evidence(ev)
        if o.get("recovery"):
            run_context.metadata["recovery_events"] = o["recovery"]
        if o.get("policy_block"):
            run_context.metadata["policy_block"] = True
        if o.get("requires_approval"):
            run_context.metadata["requires_approval"] = True
        run_context.metadata["direct_runtime"] = {"status": o["status"]}
        return run_context


class RecordingRetriever:
    """RunContext-aware retriever double — records the RunContexts it saw."""

    def __init__(self, tools=None):
        self._tools = tools or []
        self.seen = []

    def retrieve(self, request):
        return CapabilityRetrievalResponse(query=request.query, matches=[])

    def retrieve_for_run_context(self, run_context, *, top_k=5, **kw):
        self.seen.append(run_context)
        matches = [CapabilityMatch(tool=t, score=1.0) for t in self._tools]
        return CapabilityRetrievalResponse(query=run_context.user_request, matches=matches)


def planner_context(goal="do several things", **kw):
    rc = RunContext.create(goal, user_id="u", **kw)
    rc.attach_behavior_profile(
        BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi-step", confidence=0.9)
    )
    return rc


def plan(*tasks):
    return ExecutionPlan(id="plan-1", goal="goal", tasks=list(tasks))


OK = {"status": DirectStatus.SUCCESS.value, "capability": "cap", "output": {"ok": True}}
FAIL = {"status": DirectStatus.NEEDS_USER.value}


# --------------------------------------------------------------------------- #
# Path gating
# --------------------------------------------------------------------------- #

def test_planner_path_accepted():
    direct = FakeDirectRuntime([OK])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(), plan(PlannerTask(id="t1", request="first"))))
    assert rc.metadata["planner_runtime"]["runtime_status"] == RuntimeStatus.COMPLETED.value


def test_direct_path_rejected():
    rc = RunContext.create("simple", user_id="u")
    rc.attach_behavior_profile(BehaviorProfile(path=BehaviorPath.DIRECT, reason="direct"))
    with pytest.raises(NotPlannerPathError):
        run(PlannerRuntime(FakeDirectRuntime([]), RecordingRetriever()).run(
            rc, plan(PlannerTask(id="t1", request="x"))))


# --------------------------------------------------------------------------- #
# Sequential orchestration
# --------------------------------------------------------------------------- #

def test_sequential_execution_and_order():
    direct = FakeDirectRuntime([OK, OK, OK])
    tasks = plan(
        PlannerTask(id="t1", request="alpha"),
        PlannerTask(id="t2", request="beta"),
        PlannerTask(id="t3", request="gamma"),
    )
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(planner_context(), tasks))

    # DirectRuntime invoked once per task, in order.
    assert [c[0] for c in direct.calls] == ["alpha", "beta", "gamma"]
    pr = rc.metadata["planner_runtime"]
    assert pr["execution_order"] == ["t1", "t2", "t3"]
    assert pr["completed_tasks"] == ["t1", "t2", "t3"]
    assert pr["runtime_status"] == RuntimeStatus.COMPLETED.value


def test_execution_state_updated_after_every_task():
    direct = FakeDirectRuntime([OK, OK])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(PlannerTask(id="t1", request="a"), PlannerTask(id="t2", request="b")),
    ))
    state = rc.execution_state
    assert set(state.step_results) == {"t1", "t2"}
    assert state.completed_steps == ["t1", "t2"]
    assert state.get_result("t1").status == StepStatus.SUCCEEDED


def test_tool_outputs_and_evidence_aggregated_on_parent():
    ev = EvidenceItem(source="document", content="hit")
    direct = FakeDirectRuntime([
        {"status": DirectStatus.SUCCESS.value, "capability": "c1", "output": {"a": 1}, "evidence": [ev]},
        {"status": DirectStatus.SUCCESS.value, "capability": "c2", "output": {"b": 2}},
    ])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(PlannerTask(id="t1", request="a"), PlannerTask(id="t2", request="b")),
    ))
    assert [o.capability_id for o in rc.tool_outputs] == ["c1", "c2"]
    assert len(rc.evidence) == 1
    assert len(rc.metadata["execution_history"]) == 2


# --------------------------------------------------------------------------- #
# Stop / continue policy
# --------------------------------------------------------------------------- #

def test_optional_failure_continues():
    direct = FakeDirectRuntime([FAIL, OK])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(
            PlannerTask(id="t1", request="a", optional=True),
            PlannerTask(id="t2", request="b"),
        ),
    ))
    assert len(direct.calls) == 2  # continued past the optional failure
    pr = rc.metadata["planner_runtime"]
    assert pr["failed_tasks"] == ["t1"]
    assert pr["completed_tasks"] == ["t2"]
    assert pr["runtime_status"] == RuntimeStatus.COMPLETED.value


def test_required_failure_stops():
    direct = FakeDirectRuntime([FAIL, OK])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(
            PlannerTask(id="t1", request="a"),  # required
            PlannerTask(id="t2", request="b"),
        ),
    ))
    assert len(direct.calls) == 1  # stopped before t2
    pr = rc.metadata["planner_runtime"]
    assert pr["failed_tasks"] == ["t1"]
    assert pr["pending_tasks"] == ["t2"]
    assert pr["execution_order"] == ["t1"]
    assert pr["runtime_status"] == RuntimeStatus.STOPPED_REQUIRED_FAILURE.value
    assert rc.execution_state.failed_steps == ["t1"]


def test_partial_result_continues_and_recorded():
    direct = FakeDirectRuntime([
        {"status": DirectStatus.PARTIAL.value, "capability": "c1", "output": {"a": 1}},
        OK,
    ])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(PlannerTask(id="t1", request="a"), PlannerTask(id="t2", request="b")),
    ))
    pr = rc.metadata["planner_runtime"]
    assert pr["partial_tasks"] == ["t1"]
    assert pr["completed_tasks"] == ["t1", "t2"]  # partial counts as completed
    assert pr["runtime_status"] == RuntimeStatus.COMPLETED.value


def test_policy_block_stops():
    direct = FakeDirectRuntime([{"status": DirectStatus.SUCCESS.value, "policy_block": True}, OK])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(),
        plan(PlannerTask(id="t1", request="a"), PlannerTask(id="t2", request="b")),
    ))
    assert len(direct.calls) == 1
    assert rc.metadata["planner_runtime"]["runtime_status"] == RuntimeStatus.STOPPED_POLICY_BLOCK.value
    assert rc.execution_state.blocked_steps == ["t1"]


def test_recovery_events_recorded():
    direct = FakeDirectRuntime([
        {"status": DirectStatus.SUCCESS.value, "capability": "c1", "output": {},
         "recovery": [{"strategy": "retry", "capability": "c1"}]},
    ])
    rc = run(PlannerRuntime(direct, RecordingRetriever()).run(
        planner_context(), plan(PlannerTask(id="t1", request="a"))))
    events = rc.metadata["recovery_events"]
    assert events[0]["strategy"] == "retry"
    assert events[0]["task_id"] == "t1"  # tagged with the owning task
    assert rc.metadata["execution_history"][0]["recovery_events"]


# --------------------------------------------------------------------------- #
# RunContext-aware capability retrieval
# --------------------------------------------------------------------------- #

def test_retrieval_receives_run_context_with_working_context():
    retriever = RecordingRetriever(tools=[make_tool("cap")])
    wc = [WorkingContextItem(source="thread_summary", content="prior turn")]
    direct = FakeDirectRuntime([OK])
    run(PlannerRuntime(direct, retriever).run(
        planner_context(working_context=wc),
        plan(PlannerTask(id="t1", request="continue")),
    ))
    # The retriever saw a RunContext (not a raw string) carrying working context.
    assert len(retriever.seen) == 1
    seen = retriever.seen[0]
    assert isinstance(seen, RunContext)
    assert [w.content for w in seen.working_context] == ["prior turn"]


def test_real_retriever_uses_run_context_not_only_raw_request():
    # Raw request has no capability keywords; the working context does.
    retriever = KeywordCapabilityRetriever(get_default_tool_registry())
    rc = RunContext.create(
        "please help me with this",
        user_id="u",
        working_context=[WorkingContextItem(source="recent_message", content="is my document done processing")],
    )

    raw_only = retriever.retrieve(CapabilityRetrievalRequest(query=rc.user_request))
    ctx_aware = retriever.retrieve_for_run_context(rc)

    raw_ids = [m.tool.id for m in raw_only.matches if m.score > 0]
    ctx_ids = [m.tool.id for m in ctx_aware.matches if m.score > 0]
    # The job-status capability surfaces only once working context is folded in.
    assert "get_job_status" not in raw_ids
    assert "get_job_status" in ctx_ids


# --------------------------------------------------------------------------- #
# Immutability + hygiene
# --------------------------------------------------------------------------- #

def test_working_context_preserved():
    wc = [WorkingContextItem(source="thread_summary", content="prior")]
    rc = planner_context(working_context=wc)
    before = [w.content for w in rc.working_context]
    run(PlannerRuntime(FakeDirectRuntime([OK, OK]), RecordingRetriever()).run(
        rc, plan(PlannerTask(id="t1", request="a"), PlannerTask(id="t2", request="b"))))
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
    targets = _module_level_import_targets(planner_module)
    for banned in (
        "app.config", "app.services", "app.db", "motor", "redis", "qdrant", "llm",
    ):
        assert not any(banned in t for t in targets), (banned, targets)
    src = inspect.getsource(planner_module).lower()
    assert "llm_provider" not in src
    assert "llm_client" not in src
