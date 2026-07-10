"""Deterministic Policy Engine.

Evaluates a structurally-valid Plan against ToolSpec metadata and produces a
per-step decision (ALLOW / REQUIRE_APPROVAL / BLOCK). Annotation only — no HITL
interrupt, no execution. Most-restrictive-wins: BLOCK > REQUIRE_APPROVAL > ALLOW.
Does not mutate the Plan or the ToolRegistry. See docs/architecture/v2.md §7.
"""

from app.agent.models.plan import Plan, PlanStep, PlanStepType
from app.agent.models.policy import (
    PolicyDecision,
    PolicyReasonCode,
    PolicyReport,
    StepPolicyDecision,
)
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolSpec
from app.agent.registry.registry import ToolRegistry

_LEVEL = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.REQUIRE_APPROVAL: 1,
    PolicyDecision.BLOCK: 2,
}
_LEVEL_TO_DECISION = {v: k for k, v in _LEVEL.items()}


class PolicyEngine:
    def __init__(
        self,
        registry: ToolRegistry,
        user_permissions: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._user_permissions = set(user_permissions or [])

    def evaluate(self, plan: Plan) -> PolicyReport:
        return PolicyReport(
            step_decisions=[self._evaluate_step(step) for step in plan.steps]
        )

    def _evaluate_step(self, step: PlanStep) -> StepPolicyDecision:
        if step.step_type == PlanStepType.FINAL_RESPONSE:
            return StepPolicyDecision(
                step_id=step.id,
                capability_id=None,
                decision=PolicyDecision.ALLOW,
                reason_codes=[],
                requires_approval=False,
                audit_required=False,
                risk_level=None,
                message="FINAL_RESPONSE step allowed",
            )

        # Defensive blocks when the tool can't be resolved.
        if not step.capability_id:
            return self._blocked(
                step.id, None, None, [], "TOOL step missing capability_id — blocked"
            )
        if not self._registry.exists(step.capability_id):
            return self._blocked(
                step.id,
                step.capability_id,
                None,
                [],
                f"unknown capability '{step.capability_id}' — blocked",
            )

        tool = self._registry.get(step.capability_id)
        return self._evaluate_tool(step, tool)

    def _evaluate_tool(self, step: PlanStep, tool: ToolSpec) -> StepPolicyDecision:
        reasons: list[PolicyReasonCode] = []
        level = _LEVEL[PolicyDecision.ALLOW]
        audit_required = False

        # -- Blocking conditions --------------------------------------------
        if not tool.enabled:
            reasons.append(PolicyReasonCode.TOOL_DISABLED_BLOCKED)
            level = max(level, _LEVEL[PolicyDecision.BLOCK])

        if not set(tool.required_permissions) <= self._user_permissions:
            reasons.append(PolicyReasonCode.MISSING_PERMISSION_BLOCKED)
            level = max(level, _LEVEL[PolicyDecision.BLOCK])

        # -- Warnings (do not change decision level) ------------------------
        if tool.deprecated:
            reasons.append(PolicyReasonCode.TOOL_DEPRECATED_WARNING)

        # -- Risk -----------------------------------------------------------
        if tool.risk_level == RiskLevel.LOW:
            reasons.append(PolicyReasonCode.LOW_RISK_ALLOWED)
        elif tool.risk_level == RiskLevel.MEDIUM:
            reasons.append(PolicyReasonCode.MEDIUM_RISK_ALLOWED)
        elif tool.risk_level == RiskLevel.HIGH:
            reasons.append(PolicyReasonCode.HIGH_RISK_REQUIRES_APPROVAL)
            level = max(level, _LEVEL[PolicyDecision.REQUIRE_APPROVAL])

        # -- Explicit tool approval flag ------------------------------------
        if tool.requires_approval:
            reasons.append(PolicyReasonCode.APPROVAL_REQUIRED_BY_TOOL)
            level = max(level, _LEVEL[PolicyDecision.REQUIRE_APPROVAL])

        # -- Side effects ---------------------------------------------------
        if tool.side_effects == SideEffectType.WRITE:
            reasons.append(PolicyReasonCode.WRITE_ACTION_REQUIRES_AUDIT)
            audit_required = True
        elif tool.side_effects == SideEffectType.EXTERNAL:
            reasons.append(PolicyReasonCode.EXTERNAL_ACTION_REQUIRES_APPROVAL)
            level = max(level, _LEVEL[PolicyDecision.REQUIRE_APPROVAL])
            audit_required = True

        # -- Data-handling warnings -----------------------------------------
        if tool.data_egress:
            reasons.append(PolicyReasonCode.DATA_EGRESS_WARNING)
        if tool.pii_touched:
            reasons.append(PolicyReasonCode.PII_WARNING)

        decision = _LEVEL_TO_DECISION[level]
        return StepPolicyDecision(
            step_id=step.id,
            capability_id=tool.id,
            decision=decision,
            reason_codes=reasons,
            requires_approval=decision == PolicyDecision.REQUIRE_APPROVAL,
            audit_required=audit_required,
            risk_level=tool.risk_level,
            message=f"{decision.value} for capability '{tool.id}'",
        )

    @staticmethod
    def _blocked(step_id, capability_id, risk_level, reasons, message) -> StepPolicyDecision:
        return StepPolicyDecision(
            step_id=step_id,
            capability_id=capability_id,
            decision=PolicyDecision.BLOCK,
            reason_codes=reasons,
            requires_approval=False,
            audit_required=False,
            risk_level=risk_level,
            message=message,
        )
