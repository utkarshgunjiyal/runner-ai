"""Phase 10A tests — RunContext foundation."""

from app.agent.execution.state import ExecutionState
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.models.plan import FinalResponseMode, Plan, PlanStep, PlanStepType
from app.agent.runtime.context import (
    BehaviorPath,
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)


def working_items():
    return [
        WorkingContextItem(source="recent_message", content="hello there"),
        WorkingContextItem(source="thread_summary", content="we discussed X"),
    ]


def make_plan():
    return Plan(
        id="plan_1",
        user_goal="goal",
        intent="document",
        steps=[PlanStep(id="s1", step_type=PlanStepType.TOOL,
                        capability_id="search_documents", description="do s1")],
        final_response_mode=FinalResponseMode.ANSWER,
    )


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def test_initializes_correctly():
    rc = RunContext.create("what is X?", user_id="dev_user", thread_id="t1",
                           working_context=working_items())
    assert rc.user_request == "what is X?"
    assert rc.user_id == "dev_user"
    assert rc.thread_id == "t1"
    assert len(rc.working_context) == 2
    assert rc.behavior_profile is None
    assert rc.selected_capabilities == []
    assert rc.plan is None
    assert rc.tool_outputs == []
    assert rc.evidence == []
    assert rc.metadata == {}


def test_run_id_generated_and_unique():
    a = RunContext.create("q", user_id="u")
    b = RunContext.create("q", user_id="u")
    assert a.run_id and isinstance(a.run_id, str)
    assert a.run_id != b.run_id


def test_execution_state_initialized_by_default():
    rc = RunContext.create("q", user_id="u")
    assert isinstance(rc.execution_state, ExecutionState)
    assert rc.execution_state.run_id == rc.run_id


def test_execution_state_can_be_attached():
    provided = ExecutionState(run_id="custom", plan_id="p")
    rc = RunContext.create("q", user_id="u", execution_state=provided)
    assert rc.execution_state is provided

    other = ExecutionState(run_id="r2", plan_id="p2")
    rc.attach_execution_state(other)
    assert rc.execution_state is other


# --------------------------------------------------------------------------- #
# Preservation + append-only accumulation
# --------------------------------------------------------------------------- #

def test_working_context_preserved_after_appends():
    rc = RunContext.create("q", user_id="u", working_context=working_items())
    before = rc.working_context

    rc.append_tool_output(ToolOutput(capability_id="search_documents", output={"hits": []}))
    rc.append_evidence(EvidenceItem(source="chunk", content="grounding text"))

    after = rc.working_context
    assert [i.content for i in after] == [i.content for i in before]
    assert len(after) == 2


def test_working_context_property_returns_copy():
    rc = RunContext.create("q", user_id="u", working_context=working_items())
    rc.working_context.append(WorkingContextItem(source="x", content="mutated"))
    # internal working context is unaffected by mutating the returned list
    assert len(rc.working_context) == 2


def test_tool_outputs_append_without_deleting_context():
    rc = RunContext.create("q", user_id="u", working_context=working_items())
    rc.append_tool_output(ToolOutput(step_id="s1", output={"a": 1}))
    rc.append_tool_output(ToolOutput(step_id="s2", output={"b": 2}))
    assert len(rc.tool_outputs) == 2
    assert [o.step_id for o in rc.tool_outputs] == ["s1", "s2"]
    assert len(rc.working_context) == 2  # untouched
    assert rc.evidence == []             # untouched


def test_evidence_appends_in_order():
    rc = RunContext.create("q", user_id="u")
    rc.append_evidence(EvidenceItem(source="a", content="first", score=0.9))
    rc.append_evidence(EvidenceItem(source="b", content="second", score=0.5))
    assert [e.content for e in rc.evidence] == ["first", "second"]


# --------------------------------------------------------------------------- #
# Attach artifacts
# --------------------------------------------------------------------------- #

def test_attach_behavior_profile():
    rc = RunContext.create("q", user_id="u")
    profile = BehaviorProfile(path=BehaviorPath.PLANNER, reason="multi-step goal")
    rc.attach_behavior_profile(profile)
    assert rc.behavior_profile is profile
    assert rc.behavior_profile.path == BehaviorPath.PLANNER


def test_attach_selected_capabilities():
    rc = RunContext.create("q", user_id="u")
    rc.attach_selected_capabilities(["search_documents", "get_thread_summary"])
    assert rc.selected_capabilities == ["search_documents", "get_thread_summary"]


def test_attach_plan_sets_plan_and_execution_plan_id():
    rc = RunContext.create("q", user_id="u")
    plan = make_plan()
    rc.attach_plan(plan)
    assert rc.plan is plan
    assert rc.execution_state.plan_id == "plan_1"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #

def test_planner_view_contains_request_working_context_capabilities():
    rc = RunContext.create("what is X?", user_id="u", working_context=working_items())
    rc.attach_selected_capabilities(["search_documents"])
    view = rc.planner_view()
    assert view["user_request"] == "what is X?"
    assert len(view["working_context"]) == 2
    assert view["selected_capabilities"] == ["search_documents"]
    # planner view does not leak tool outputs / evidence
    assert "tool_outputs" not in view
    assert "evidence" not in view


def test_final_response_view_contains_request_context_outputs_evidence():
    rc = RunContext.create("what is X?", user_id="u", working_context=working_items())
    rc.append_tool_output(ToolOutput(capability_id="search_documents", output={"hits": [1]}))
    rc.append_evidence(EvidenceItem(source="chunk", content="grounding"))
    view = rc.final_response_view()
    assert view["user_request"] == "what is X?"
    assert len(view["working_context"]) == 2
    assert len(view["tool_outputs"]) == 1
    assert view["tool_outputs"][0]["output"] == {"hits": [1]}
    assert len(view["evidence"]) == 1
    assert view["evidence"][0]["content"] == "grounding"


def test_run_context_composes_execution_state_results():
    # ExecutionState remains fully usable inside RunContext.
    rc = RunContext.create("q", user_id="u")
    rc.execution_state.record_result(
        StepExecutionResult(step_id="s1", status=StepStatus.SUCCEEDED, output={"ok": True})
    )
    assert rc.execution_state.completed_steps == ["s1"]
    assert rc.execution_state.get_result("s1").output == {"ok": True}
