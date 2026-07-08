"""Phase 3 tests — Plan / DAG models."""

import pytest
from pydantic import ValidationError

from app.agent.models.plan import (
    ArgBinding,
    FinalResponseMode,
    Plan,
    PlanStep,
    PlanStepType,
    StepNotFoundError,
)


def tool_step(step_id, capability_id="search_documents", depends_on=None, **kw):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id,
        description=f"do {step_id}",
        depends_on=depends_on or [],
        **kw,
    )


def final_step(step_id="final", depends_on=None):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.FINAL_RESPONSE,
        description="respond",
        depends_on=depends_on or [],
    )


def make_plan(steps, **kw):
    base = dict(
        id="plan_1",
        user_goal="do the thing",
        intent="document",
        steps=steps,
        final_response_mode=FinalResponseMode.ANSWER,
    )
    base.update(kw)
    return Plan(**base)


# --------------------------------------------------------------------------- #
# Valid plans
# --------------------------------------------------------------------------- #

def test_valid_linear_plan():
    plan = make_plan([
        tool_step("step_1"),
        tool_step("step_2", depends_on=["step_1"]),
        final_step("final", depends_on=["step_2"]),
    ])
    assert len(plan.steps) == 3
    assert plan.final_response_mode == FinalResponseMode.ANSWER


def test_valid_dag_with_two_independent_roots():
    plan = make_plan([
        tool_step("a"),
        tool_step("b"),
        tool_step("c", depends_on=["a", "b"]),
    ])
    assert {s.id for s in plan.root_steps()} == {"a", "b"}
    assert [s.id for s in plan.terminal_steps()] == ["c"]


def test_args_can_contain_binding_string():
    step = tool_step("step_2", depends_on=["step_1"], args={"query": "${step_1.output.summary}"})
    assert step.args["query"] == "${step_1.output.summary}"


def test_arg_binding_model():
    binding = ArgBinding(step_id="step_1", path="output.summary")
    assert binding.step_id == "step_1"
    assert binding.path == "output.summary"


# --------------------------------------------------------------------------- #
# Structural validation failures
# --------------------------------------------------------------------------- #

def test_duplicate_step_ids_fail():
    with pytest.raises(ValidationError):
        make_plan([tool_step("x"), tool_step("x")])


def test_dependency_on_unknown_step_fails():
    with pytest.raises(ValidationError):
        make_plan([tool_step("step_1", depends_on=["ghost"])])


def test_self_dependency_fails():
    with pytest.raises(ValidationError):
        tool_step("step_1", depends_on=["step_1"])


def test_dependency_on_later_step_fails():
    with pytest.raises(ValidationError):
        make_plan([tool_step("s1", depends_on=["s2"]), tool_step("s2")])


def test_simple_cycle_fails():
    # A→B and B→A cannot both be satisfied under the ordering rule.
    with pytest.raises(ValidationError):
        make_plan([tool_step("a", depends_on=["b"]), tool_step("b", depends_on=["a"])])


def test_tool_step_without_capability_id_fails():
    with pytest.raises(ValidationError):
        PlanStep(id="x", step_type=PlanStepType.TOOL, description="d")


def test_final_response_step_without_capability_id_succeeds():
    step = final_step("final")
    assert step.step_type == PlanStepType.FINAL_RESPONSE
    assert step.capability_id is None


def test_empty_plan_fails():
    with pytest.raises(ValidationError):
        make_plan([])


def test_empty_identifier_fields_fail():
    with pytest.raises(ValidationError):
        make_plan([tool_step("s1")], id="  ")
    with pytest.raises(ValidationError):
        make_plan([tool_step("s1")], user_goal="")
    with pytest.raises(ValidationError):
        make_plan([tool_step("s1")], intent="")
    with pytest.raises(ValidationError):
        PlanStep(id="", step_type=PlanStepType.FINAL_RESPONSE, description="d")
    with pytest.raises(ValidationError):
        PlanStep(id="x", step_type=PlanStepType.FINAL_RESPONSE, description="   ")


def test_duplicate_depends_on_normalized():
    step = tool_step("step_3", depends_on=["a", "a", "b", "a"])
    assert step.depends_on == ["a", "b"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_get_step():
    plan = make_plan([tool_step("a"), tool_step("b", depends_on=["a"])])
    assert plan.get_step("b").id == "b"


def test_get_step_unknown_raises():
    plan = make_plan([tool_step("a")])
    with pytest.raises(StepNotFoundError):
        plan.get_step("nope")


def test_dependency_graph():
    plan = make_plan([
        tool_step("a"),
        tool_step("b"),
        tool_step("c", depends_on=["a", "b"]),
    ])
    assert plan.dependency_graph() == {"a": [], "b": [], "c": ["a", "b"]}


def test_root_steps():
    plan = make_plan([
        tool_step("a"),
        tool_step("b", depends_on=["a"]),
        tool_step("c"),
    ])
    assert [s.id for s in plan.root_steps()] == ["a", "c"]


def test_terminal_steps():
    plan = make_plan([
        tool_step("a"),
        tool_step("b", depends_on=["a"]),
        final_step("final", depends_on=["b"]),
    ])
    assert [s.id for s in plan.terminal_steps()] == ["final"]
