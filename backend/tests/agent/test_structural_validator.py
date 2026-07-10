"""Phase 4 tests — structural plan validator."""

from app.agent.models.plan import FinalResponseMode, Plan, PlanStep, PlanStepType
from app.agent.models.tool_spec import (
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.models.validation import ValidationSeverity
from app.agent.registry.registry import ToolRegistry
from app.agent.validation.structural_validator import StructuralPlanValidator


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #

def make_tool(tool_id, *, enabled=True, input_schema=None, output_fields=None,
              output_schema=None) -> ToolSpec:
    return ToolSpec(
        id=tool_id,
        name=tool_id,
        kind=ToolKind.INTERNAL,
        description=f"{tool_id} tool",
        input_schema=input_schema if input_schema is not None else {},
        output_schema=output_schema if output_schema is not None else {},
        output_fields=output_fields or [],
        risk_level=RiskLevel.LOW,
        side_effects=SideEffectType.READ,
        requires_approval=False,
        enabled=enabled,
    )


def registry_with(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


SEARCH = make_tool(
    "search_documents",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"],
    },
    output_schema={"type": "object", "properties": {"hits": {"type": "array"}}},
    output_fields=["hits"],
)

CONSUMER = make_tool(
    "consumer",
    input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    output_fields=["answer"],
)


def tool_step(step_id, capability_id, args=None, depends_on=None):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id,
        description=f"do {step_id}",
        args=args or {},
        depends_on=depends_on or [],
    )


def final_step(step_id="final", depends_on=None):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.FINAL_RESPONSE,
        description="respond",
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


def codes(report):
    return {issue.code for issue in report.issues}


# --------------------------------------------------------------------------- #
# Passing plans
# --------------------------------------------------------------------------- #

def test_valid_plan_passes():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([tool_step("s1", "search_documents", args={"query": "hello"})])
    report = validator.validate(plan)
    assert not report.has_errors
    assert report.error_count == 0


def test_final_response_step_without_capability_passes():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "x"}),
        final_step("final", depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert not report.has_errors


def test_valid_binding_passes():
    validator = StructuralPlanValidator(registry_with(SEARCH, CONSUMER))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "hi"}),
        tool_step("s2", "consumer", args={"q": "${s1.output.hits}"}, depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert not report.has_errors, codes(report)


# --------------------------------------------------------------------------- #
# Capability / args errors
# --------------------------------------------------------------------------- #

def test_unknown_capability_error():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([tool_step("s1", "ghost_tool", args={})])
    report = validator.validate(plan)
    assert "UNKNOWN_CAPABILITY" in codes(report)
    assert report.has_errors


def test_disabled_tool_error():
    disabled = make_tool("disabled_tool", enabled=False)
    validator = StructuralPlanValidator(registry_with(disabled))
    plan = make_plan([tool_step("s1", "disabled_tool")])
    report = validator.validate(plan)
    assert "DISABLED_TOOL" in codes(report)


def test_missing_required_arg_error():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([tool_step("s1", "search_documents", args={})])
    report = validator.validate(plan)
    assert "MISSING_REQUIRED_ARG" in codes(report)
    assert report.has_errors


def test_wrong_arg_type_error():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([tool_step("s1", "search_documents", args={"query": 123})])
    report = validator.validate(plan)
    assert "WRONG_ARG_TYPE" in codes(report)


def test_extra_unknown_arg_is_warning():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "ok", "bogus": 1}),
    ])
    report = validator.validate(plan)
    assert "UNKNOWN_ARG" in codes(report)
    assert report.has_warnings
    assert not report.has_errors  # query is valid; the extra arg is only a warning


# --------------------------------------------------------------------------- #
# Binding errors
# --------------------------------------------------------------------------- #

def test_binding_to_unknown_step_error():
    validator = StructuralPlanValidator(registry_with(SEARCH, CONSUMER))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "hi"}),
        tool_step("s2", "consumer", args={"q": "${ghost.output.hits}"}, depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert "UNKNOWN_BINDING_STEP" in codes(report)


def test_malformed_binding_error():
    validator = StructuralPlanValidator(registry_with(SEARCH, CONSUMER))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "hi"}),
        tool_step("s2", "consumer", args={"q": "${s1}"}, depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert "MALFORMED_BINDING" in codes(report)


def test_binding_path_not_output_error():
    validator = StructuralPlanValidator(registry_with(SEARCH, CONSUMER))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "hi"}),
        tool_step("s2", "consumer", args={"q": "${s1.result.hits}"}, depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert "BINDING_INVALID_PATH" in codes(report)


def test_binding_missing_output_field_error():
    validator = StructuralPlanValidator(registry_with(SEARCH, CONSUMER))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"query": "hi"}),
        tool_step("s2", "consumer", args={"q": "${s1.output.nope}"}, depends_on=["s1"]),
    ])
    report = validator.validate(plan)
    assert "BINDING_UNKNOWN_OUTPUT_FIELD" in codes(report)


# --------------------------------------------------------------------------- #
# Aggregation / counts
# --------------------------------------------------------------------------- #

def test_validator_collects_multiple_issues():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([
        tool_step("s1", "ghost_tool"),                          # UNKNOWN_CAPABILITY
        tool_step("s2", "search_documents", args={"query": 5}),  # WRONG_ARG_TYPE
    ])
    report = validator.validate(plan)
    assert report.error_count >= 2
    assert {"UNKNOWN_CAPABILITY", "WRONG_ARG_TYPE"} <= codes(report)


def test_report_counts():
    validator = StructuralPlanValidator(registry_with(SEARCH))
    plan = make_plan([
        tool_step("s1", "search_documents", args={"bogus": 1}),  # missing query (E) + unknown arg (W)
    ])
    report = validator.validate(plan)
    assert report.error_count == 1
    assert report.warning_count == 1
    assert report.has_errors
    assert report.has_warnings
    severities = {i.severity for i in report.issues}
    assert ValidationSeverity.ERROR in severities
    assert ValidationSeverity.WARNING in severities
