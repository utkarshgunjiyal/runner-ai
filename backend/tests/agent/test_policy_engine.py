"""Phase 5 tests — Policy Engine."""

from app.agent.models.plan import FinalResponseMode, Plan, PlanStep, PlanStepType
from app.agent.models.policy import PolicyDecision, PolicyReasonCode
from app.agent.models.tool_spec import (
    RiskLevel,
    SideEffectType,
    ToolKind,
    ToolSpec,
)
from app.agent.policy.engine import PolicyEngine
from app.agent.registry.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def make_tool(
    tool_id,
    *,
    risk=RiskLevel.LOW,
    side_effects=SideEffectType.READ,
    requires_approval=False,
    enabled=True,
    deprecated=False,
    required_permissions=None,
    data_egress=False,
    pii_touched=False,
) -> ToolSpec:
    return ToolSpec(
        id=tool_id,
        name=tool_id,
        kind=ToolKind.INTERNAL,
        description=f"{tool_id} tool",
        input_schema={},
        output_schema={},
        risk_level=risk,
        side_effects=side_effects,
        requires_approval=requires_approval,
        enabled=enabled,
        deprecated=deprecated,
        required_permissions=required_permissions or [],
        data_egress=data_egress,
        pii_touched=pii_touched,
    )


def registry_with(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def tool_step(step_id, capability_id):
    return PlanStep(
        id=step_id,
        step_type=PlanStepType.TOOL,
        capability_id=capability_id,
        description=f"do {step_id}",
    )


def final_step(step_id="final"):
    return PlanStep(id=step_id, step_type=PlanStepType.FINAL_RESPONSE, description="respond")


def make_plan(steps):
    return Plan(
        id="plan_1",
        user_goal="goal",
        intent="document",
        steps=steps,
        final_response_mode=FinalResponseMode.ANSWER,
    )


def evaluate_single(tool, *, user_permissions=None):
    engine = PolicyEngine(registry_with(tool), user_permissions=user_permissions)
    report = engine.evaluate(make_plan([tool_step("s1", tool.id)]))
    return report.step_decisions[0]


# --------------------------------------------------------------------------- #
# Risk / approval
# --------------------------------------------------------------------------- #

def test_low_read_tool_allowed():
    d = evaluate_single(make_tool("t", risk=RiskLevel.LOW, side_effects=SideEffectType.READ))
    assert d.decision == PolicyDecision.ALLOW
    assert d.requires_approval is False
    assert d.audit_required is False
    assert PolicyReasonCode.LOW_RISK_ALLOWED in d.reason_codes


def test_medium_write_tool_allowed_but_audit_required():
    d = evaluate_single(
        make_tool("t", risk=RiskLevel.MEDIUM, side_effects=SideEffectType.WRITE)
    )
    assert d.decision == PolicyDecision.ALLOW
    assert d.audit_required is True
    assert PolicyReasonCode.MEDIUM_RISK_ALLOWED in d.reason_codes
    assert PolicyReasonCode.WRITE_ACTION_REQUIRES_AUDIT in d.reason_codes


def test_high_risk_requires_approval():
    # HIGH risk forces requires_approval=True at the ToolSpec level too.
    d = evaluate_single(make_tool("t", risk=RiskLevel.HIGH, requires_approval=True))
    assert d.decision == PolicyDecision.REQUIRE_APPROVAL
    assert d.requires_approval is True
    assert PolicyReasonCode.HIGH_RISK_REQUIRES_APPROVAL in d.reason_codes


def test_requires_approval_flag_forces_approval():
    d = evaluate_single(make_tool("t", risk=RiskLevel.LOW, requires_approval=True))
    assert d.decision == PolicyDecision.REQUIRE_APPROVAL
    assert PolicyReasonCode.APPROVAL_REQUIRED_BY_TOOL in d.reason_codes


def test_external_side_effect_requires_approval_and_audit():
    d = evaluate_single(make_tool("t", side_effects=SideEffectType.EXTERNAL))
    assert d.decision == PolicyDecision.REQUIRE_APPROVAL
    assert d.audit_required is True
    assert PolicyReasonCode.EXTERNAL_ACTION_REQUIRES_APPROVAL in d.reason_codes


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #

def test_disabled_tool_blocked():
    d = evaluate_single(make_tool("t", enabled=False))
    assert d.decision == PolicyDecision.BLOCK
    assert PolicyReasonCode.TOOL_DISABLED_BLOCKED in d.reason_codes


def test_deprecated_tool_allowed_with_warning():
    d = evaluate_single(make_tool("t", deprecated=True))
    assert d.decision == PolicyDecision.ALLOW
    assert PolicyReasonCode.TOOL_DEPRECATED_WARNING in d.reason_codes


def test_missing_permission_blocks():
    d = evaluate_single(
        make_tool("t", required_permissions=["admin"]), user_permissions=[]
    )
    assert d.decision == PolicyDecision.BLOCK
    assert PolicyReasonCode.MISSING_PERMISSION_BLOCKED in d.reason_codes


def test_permission_present_allows():
    d = evaluate_single(
        make_tool("t", required_permissions=["admin"]), user_permissions=["admin"]
    )
    assert d.decision == PolicyDecision.ALLOW


def test_unknown_capability_blocks():
    engine = PolicyEngine(registry_with(make_tool("real")))
    report = engine.evaluate(make_plan([tool_step("s1", "ghost")]))
    d = report.step_decisions[0]
    assert d.decision == PolicyDecision.BLOCK


def test_missing_capability_on_tool_step_blocks():
    # A validated Plan can never carry a TOOL step without a capability_id, so
    # this defensive branch is unit-tested directly on the engine. We bypass
    # PlanStep validation with model_construct to reach it.
    bad_step = PlanStep.model_construct(
        id="s1",
        step_type=PlanStepType.TOOL,
        capability_id=None,
        description="broken",
        args={},
        depends_on=[],
        output_alias=None,
        parallel_group=None,
    )
    engine = PolicyEngine(registry_with(make_tool("real")))
    decision = engine._evaluate_step(bad_step)
    assert decision.decision == PolicyDecision.BLOCK


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #

def test_data_egress_adds_warning():
    d = evaluate_single(make_tool("t", data_egress=True))
    assert PolicyReasonCode.DATA_EGRESS_WARNING in d.reason_codes


def test_pii_adds_warning():
    d = evaluate_single(make_tool("t", pii_touched=True))
    assert PolicyReasonCode.PII_WARNING in d.reason_codes


# --------------------------------------------------------------------------- #
# Final response + aggregation
# --------------------------------------------------------------------------- #

def test_final_response_allowed_without_capability():
    engine = PolicyEngine(registry_with(make_tool("t")))
    report = engine.evaluate(make_plan([tool_step("s1", "t"), final_step("final")]))
    final_decision = report.step_decisions[-1]
    assert final_decision.decision == PolicyDecision.ALLOW
    assert final_decision.capability_id is None
    assert final_decision.requires_approval is False
    assert final_decision.audit_required is False


def test_most_restrictive_wins_block_over_approval():
    # HIGH risk (approval) + missing permission (block) → BLOCK.
    d = evaluate_single(
        make_tool("t", risk=RiskLevel.HIGH, requires_approval=True,
                  required_permissions=["admin"]),
        user_permissions=[],
    )
    assert d.decision == PolicyDecision.BLOCK
    assert PolicyReasonCode.MISSING_PERMISSION_BLOCKED in d.reason_codes
    assert PolicyReasonCode.HIGH_RISK_REQUIRES_APPROVAL in d.reason_codes


def test_report_properties():
    reg = registry_with(
        make_tool("low", risk=RiskLevel.LOW),
        make_tool("high", risk=RiskLevel.HIGH, requires_approval=True),
        make_tool("blocked", enabled=False),
    )
    engine = PolicyEngine(reg)
    report = engine.evaluate(make_plan([
        tool_step("s_low", "low"),
        tool_step("s_high", "high"),
        tool_step("s_blocked", "blocked"),
        final_step("final"),
    ]))
    assert report.is_blocked is True
    assert report.requires_approval is True
    assert [d.step_id for d in report.blocked_steps] == ["s_blocked"]
    assert [d.step_id for d in report.approval_steps] == ["s_high"]
    assert {d.step_id for d in report.allowed_steps} == {"s_low", "final"}


def test_policy_evaluates_all_steps():
    reg = registry_with(make_tool("a"), make_tool("b"))
    engine = PolicyEngine(reg)
    report = engine.evaluate(make_plan([
        tool_step("s1", "a"),
        tool_step("s2", "b"),
        final_step("final"),
    ]))
    assert len(report.step_decisions) == 3
    assert [d.step_id for d in report.step_decisions] == ["s1", "s2", "final"]
