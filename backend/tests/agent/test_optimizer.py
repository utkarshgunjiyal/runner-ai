"""Phase 6 tests — plan optimizer (execution grouping + annotations)."""

from app.agent.models.optimization import OptimizationType
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
    # Default to a unique capability per step so grouping tests don't create
    # accidental duplicates; duplicate tests pass an explicit shared capability.
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id or f"tool_{step_id}",
        description=f"do {step_id}",
        args=args or {},
        depends_on=depends_on or [],
    )


def make_plan(steps, plan_id="plan_1"):
    return Plan(
        id=plan_id,
        user_goal="goal",
        intent="document",
        steps=steps,
        final_response_mode=FinalResponseMode.ANSWER,
    )


def note_types(report):
    return {n.type for n in report.notes}


# --------------------------------------------------------------------------- #
# Execution grouping
# --------------------------------------------------------------------------- #

def test_linear_plan_creates_sequential_groups():
    plan = make_plan([
        tool_step("s1"),
        tool_step("s2", depends_on=["s1"]),
        tool_step("s3", depends_on=["s2"]),
    ])
    opt, _ = PlanOptimizer().optimize(plan)
    assert [g.group_id for g in opt.execution_groups] == ["group_1", "group_2", "group_3"]
    assert [g.step_ids for g in opt.execution_groups] == [["s1"], ["s2"], ["s3"]]
    assert all(g.parallel is False for g in opt.execution_groups)


def test_two_independent_roots_create_parallel_first_group():
    plan = make_plan([
        tool_step("a"),
        tool_step("b"),
        tool_step("c", depends_on=["a", "b"]),
    ])
    opt, report = PlanOptimizer().optimize(plan)
    assert opt.execution_groups[0].step_ids == ["a", "b"]
    assert opt.execution_groups[0].parallel is True
    assert opt.execution_groups[1].step_ids == ["c"]
    assert opt.execution_groups[1].parallel is False
    assert report.has_parallel_groups is True


def test_multi_level_dag_groups():
    plan = make_plan([
        tool_step("a"),
        tool_step("b"),
        tool_step("c", depends_on=["a"]),
        tool_step("d", depends_on=["a", "b"]),
        tool_step("e", depends_on=["c", "d"]),
    ])
    opt, _ = PlanOptimizer().optimize(plan)
    assert [g.step_ids for g in opt.execution_groups] == [["a", "b"], ["c", "d"], ["e"]]
    assert [g.parallel for g in opt.execution_groups] == [True, True, False]


def test_parallel_flag_only_when_group_has_more_than_one_step():
    plan = make_plan([tool_step("only")])
    opt, _ = PlanOptimizer().optimize(plan)
    assert len(opt.execution_groups) == 1
    assert opt.execution_groups[0].parallel is False


def test_execution_group_ids_deterministic():
    plan = make_plan([tool_step("a"), tool_step("b"), tool_step("c", depends_on=["a", "b"])])
    opt1, _ = PlanOptimizer().optimize(plan)
    opt2, _ = PlanOptimizer().optimize(plan)
    assert [g.group_id for g in opt1.execution_groups] == ["group_1", "group_2"]
    assert [(g.group_id, g.step_ids) for g in opt1.execution_groups] == [
        (g.group_id, g.step_ids) for g in opt2.execution_groups
    ]


# --------------------------------------------------------------------------- #
# Non-mutation / preservation
# --------------------------------------------------------------------------- #

def test_original_plan_not_mutated():
    plan = make_plan([tool_step("a"), tool_step("b", depends_on=["a"])])
    before_ids = [s.id for s in plan.steps]
    before_plan_id = plan.id
    PlanOptimizer().optimize(plan)
    assert [s.id for s in plan.steps] == before_ids
    assert plan.id == before_plan_id


def test_optimized_plan_preserves_all_original_steps():
    plan = make_plan([
        tool_step("a"),
        tool_step("b", depends_on=["a"]),
        tool_step("c", depends_on=["b"]),
    ])
    opt, _ = PlanOptimizer().optimize(plan)
    assert opt.original_plan_id == "plan_1"
    assert [s.id for s in opt.steps] == ["a", "b", "c"]
    assert len(opt.steps) == len(plan.steps)


# --------------------------------------------------------------------------- #
# Policy-driven notes
# --------------------------------------------------------------------------- #

def _policy_report(**by_step):
    decisions = [
        StepPolicyDecision(
            step_id=step_id,
            capability_id="search",
            decision=decision,
        )
        for step_id, decision in by_step.items()
    ]
    return PolicyReport(step_decisions=decisions)


def test_blocked_step_produces_note():
    plan = make_plan([tool_step("a"), tool_step("b", depends_on=["a"])])
    report_policy = _policy_report(a=PolicyDecision.BLOCK, b=PolicyDecision.ALLOW)
    opt, report = PlanOptimizer().optimize(plan, report_policy)
    assert OptimizationType.BLOCKED_STEP_PRESERVED in note_types(report)
    # blocked step still present
    assert "a" in [s.id for s in opt.steps]


def test_approval_step_produces_note():
    plan = make_plan([tool_step("a")])
    report_policy = _policy_report(a=PolicyDecision.REQUIRE_APPROVAL)
    _, report = PlanOptimizer().optimize(plan, report_policy)
    assert OptimizationType.APPROVAL_STEP_MARKED in note_types(report)


def test_optimizer_works_without_policy_report():
    plan = make_plan([tool_step("a"), tool_step("b", depends_on=["a"])])
    opt, report = PlanOptimizer().optimize(plan)
    assert opt.execution_groups
    assert OptimizationType.BLOCKED_STEP_PRESERVED not in note_types(report)
    assert OptimizationType.APPROVAL_STEP_MARKED not in note_types(report)


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #

def test_duplicate_tool_steps_produce_note():
    plan = make_plan([
        tool_step("s1", capability_id="search", args={"query": "x"}),
        tool_step("s2", capability_id="search", args={"query": "x"}),
    ])
    _, report = PlanOptimizer().optimize(plan)
    dup_notes = [n for n in report.notes if n.type == OptimizationType.DUPLICATE_STEP_DETECTED]
    assert len(dup_notes) == 1
    assert set(dup_notes[0].step_ids) == {"s1", "s2"}


def test_different_args_are_not_duplicates():
    plan = make_plan([
        tool_step("s1", capability_id="search", args={"query": "x"}),
        tool_step("s2", capability_id="search", args={"query": "y"}),
    ])
    _, report = PlanOptimizer().optimize(plan)
    assert OptimizationType.DUPLICATE_STEP_DETECTED not in note_types(report)


# --------------------------------------------------------------------------- #
# No-op + report properties
# --------------------------------------------------------------------------- #

def test_no_op_note_when_nothing_to_do():
    # Single-step plan, no policy, no duplicates, no parallel group.
    plan = make_plan([tool_step("only")])
    _, report = PlanOptimizer().optimize(plan)
    assert report.note_count == 1
    assert report.notes[0].type == OptimizationType.NO_OP


def test_report_properties():
    plan = make_plan([tool_step("a"), tool_step("b"), tool_step("c", depends_on=["a", "b"])])
    _, report = PlanOptimizer().optimize(plan)
    assert report.has_parallel_groups is True
    assert report.parallel_group_count == 1
    assert report.note_count == 1  # the single parallel-grouping note
