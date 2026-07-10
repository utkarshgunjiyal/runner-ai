"""Phase 7 tests — executor + shared execution state."""

import pytest

from app.agent.execution.executor import PlanExecutor
from app.agent.execution.runner import FakeToolRunner, ToolRunner
from app.agent.execution.state import ExecutionState, StepResultNotFoundError
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.models.plan import FinalResponseMode, Plan, PlanStep, PlanStepType
from app.agent.models.policy import (
    PolicyDecision,
    PolicyReport,
    StepPolicyDecision,
)
from app.agent.optimization.optimizer import PlanOptimizer


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def tool_step(step_id, capability_id=None, args=None, depends_on=None):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id or f"tool_{step_id}",
        description=f"do {step_id}",
        args=args or {},
        depends_on=depends_on or [],
    )


def make_plan(steps):
    return Plan(
        id="plan_1",
        user_goal="goal",
        intent="document",
        steps=steps,
        final_response_mode=FinalResponseMode.ANSWER,
    )


def optimize(steps):
    return PlanOptimizer().optimize(make_plan(steps))[0]


def policy(**by_step):
    return PolicyReport(
        step_decisions=[
            StepPolicyDecision(step_id=sid, capability_id=f"tool_{sid}", decision=dec)
            for sid, dec in by_step.items()
        ]
    )


# --------------------------------------------------------------------------- #
# Happy path + state
# --------------------------------------------------------------------------- #

def test_executes_linear_plan_successfully():
    opt = optimize([tool_step("s1"), tool_step("s2", depends_on=["s1"]),
                    tool_step("s3", depends_on=["s2"])])
    state = PlanExecutor(FakeToolRunner()).execute(opt)
    assert state.completed_steps == ["s1", "s2", "s3"]
    assert all(state.get_result(s).status == StepStatus.SUCCEEDED for s in ["s1", "s2", "s3"])


def test_stores_outputs_in_state():
    runner = FakeToolRunner(outputs={"s1": {"summary": "hi"}})
    opt = optimize([tool_step("s1")])
    state = PlanExecutor(runner).execute(opt)
    assert state.get_result("s1").output == {"summary": "hi"}


def test_fake_runner_default_output():
    runner = FakeToolRunner()
    opt = optimize([tool_step("s1", args={"a": 1})])
    state = PlanExecutor(runner).execute(opt)
    assert state.get_result("s1").output == {"ok": True, "step_id": "s1", "args": {"a": 1}}


def test_fake_runner_configured_output():
    runner = FakeToolRunner(outputs={"s1": {"custom": 42}})
    opt = optimize([tool_step("s1")])
    state = PlanExecutor(runner).execute(opt)
    assert state.get_result("s1").output == {"custom": 42}


# --------------------------------------------------------------------------- #
# Binding resolution
# --------------------------------------------------------------------------- #

def test_binding_passes_prior_output_into_later_step():
    runner = FakeToolRunner(outputs={"s1": {"summary": "hello"}})
    opt = optimize([
        tool_step("s1"),
        tool_step("s2", args={"text": "${s1.output.summary}"}, depends_on=["s1"]),
    ])
    state = PlanExecutor(runner).execute(opt)
    assert state.get_result("s2").input["text"] == "hello"
    assert state.get_result("s2").output["args"]["text"] == "hello"


def test_missing_binding_step_causes_failed():
    opt = optimize([tool_step("s1", args={"x": "${ghost.output.y}"})])
    state = PlanExecutor(FakeToolRunner()).execute(opt)
    assert state.get_result("s1").status == StepStatus.FAILED
    assert "unknown step" in state.get_result("s1").error


def test_missing_binding_field_causes_failed():
    runner = FakeToolRunner(outputs={"s1": {"summary": "hi"}})
    opt = optimize([
        tool_step("s1"),
        tool_step("s2", args={"x": "${s1.output.missing}"}, depends_on=["s1"]),
    ])
    state = PlanExecutor(runner).execute(opt)
    assert state.get_result("s1").status == StepStatus.SUCCEEDED
    assert state.get_result("s2").status == StepStatus.FAILED


# --------------------------------------------------------------------------- #
# Policy-driven behavior
# --------------------------------------------------------------------------- #

def test_blocked_step_not_executed():
    runner = FakeToolRunner()
    opt = optimize([tool_step("s1")])
    state = PlanExecutor(runner).execute(opt, policy(s1=PolicyDecision.BLOCK))
    assert state.get_result("s1").status == StepStatus.BLOCKED
    assert "s1" not in runner.calls
    assert state.blocked_steps == ["s1"]


def test_approval_step_not_executed():
    runner = FakeToolRunner()
    opt = optimize([tool_step("s1")])
    state = PlanExecutor(runner).execute(opt, policy(s1=PolicyDecision.REQUIRE_APPROVAL))
    assert state.get_result("s1").status == StepStatus.AWAITING_APPROVAL
    assert "s1" not in runner.calls
    assert state.awaiting_approval_steps == ["s1"]


def test_dependent_skipped_if_dependency_failed():
    opt = optimize([
        tool_step("s1", args={"x": "${ghost.output.y}"}),  # fails
        tool_step("s2", depends_on=["s1"]),
    ])
    state = PlanExecutor(FakeToolRunner()).execute(opt)
    assert state.get_result("s1").status == StepStatus.FAILED
    assert state.get_result("s2").status == StepStatus.SKIPPED


def test_dependent_skipped_if_dependency_blocked():
    runner = FakeToolRunner()
    opt = optimize([tool_step("s1"), tool_step("s2", depends_on=["s1"])])
    state = PlanExecutor(runner).execute(opt, policy(s1=PolicyDecision.BLOCK))
    assert state.get_result("s2").status == StepStatus.SKIPPED
    assert "s2" not in runner.calls


def test_dependent_skipped_if_dependency_awaiting_approval():
    runner = FakeToolRunner()
    opt = optimize([tool_step("s1"), tool_step("s2", depends_on=["s1"])])
    state = PlanExecutor(runner).execute(opt, policy(s1=PolicyDecision.REQUIRE_APPROVAL))
    assert state.get_result("s2").status == StepStatus.SKIPPED


# --------------------------------------------------------------------------- #
# Ordering + failures
# --------------------------------------------------------------------------- #

def test_execution_groups_processed_in_order():
    runner = FakeToolRunner()
    opt = optimize([
        tool_step("a"),
        tool_step("b"),
        tool_step("c", depends_on=["a", "b"]),
    ])
    PlanExecutor(runner).execute(opt)
    assert runner.calls == ["a", "b", "c"]


class ExplodingRunner(ToolRunner):
    def run(self, step, args):
        raise RuntimeError("boom")


def test_tool_runner_exception_records_failed():
    opt = optimize([tool_step("s1")])
    state = PlanExecutor(ExplodingRunner()).execute(opt)
    assert state.get_result("s1").status == StepStatus.FAILED
    assert "boom" in state.get_result("s1").error


# --------------------------------------------------------------------------- #
# ExecutionState unit
# --------------------------------------------------------------------------- #

def test_record_result_updates_bucket_lists():
    state = ExecutionState(run_id="r", plan_id="p")
    for sid, status in [
        ("a", StepStatus.SUCCEEDED),
        ("b", StepStatus.FAILED),
        ("c", StepStatus.BLOCKED),
        ("d", StepStatus.AWAITING_APPROVAL),
        ("e", StepStatus.SKIPPED),
    ]:
        state.record_result(StepExecutionResult(step_id=sid, status=status))
    assert state.completed_steps == ["a"]
    assert state.failed_steps == ["b"]
    assert state.blocked_steps == ["c"]
    assert state.awaiting_approval_steps == ["d"]
    assert state.skipped_steps == ["e"]
    assert state.has_result("a") is True
    assert state.has_result("z") is False


def test_get_result_unknown_step_raises():
    state = ExecutionState(run_id="r", plan_id="p")
    with pytest.raises(StepResultNotFoundError):
        state.get_result("nope")
