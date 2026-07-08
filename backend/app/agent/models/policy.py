"""Policy evaluation models.

The Policy Engine annotates each step with a decision (ALLOW / REQUIRE_APPROVAL
/ BLOCK). It does NOT interrupt or execute — HITL enforcement happens later in
the Executor. See docs/architecture/v2.md §7.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models.tool_spec import RiskLevel


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class PolicyReasonCode(str, Enum):
    LOW_RISK_ALLOWED = "low_risk_allowed"
    MEDIUM_RISK_ALLOWED = "medium_risk_allowed"
    HIGH_RISK_REQUIRES_APPROVAL = "high_risk_requires_approval"
    APPROVAL_REQUIRED_BY_TOOL = "approval_required_by_tool"
    WRITE_ACTION_REQUIRES_AUDIT = "write_action_requires_audit"
    EXTERNAL_ACTION_REQUIRES_APPROVAL = "external_action_requires_approval"
    TOOL_DISABLED_BLOCKED = "tool_disabled_blocked"
    TOOL_DEPRECATED_WARNING = "tool_deprecated_warning"
    MISSING_PERMISSION_BLOCKED = "missing_permission_blocked"
    DATA_EGRESS_WARNING = "data_egress_warning"
    PII_WARNING = "pii_warning"


class StepPolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    capability_id: str | None
    decision: PolicyDecision
    reason_codes: list[PolicyReasonCode] = Field(default_factory=list)
    requires_approval: bool = False
    audit_required: bool = False
    risk_level: RiskLevel | None = None
    message: str = ""


class PolicyReport(BaseModel):
    step_decisions: list[StepPolicyDecision] = Field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return any(d.decision == PolicyDecision.BLOCK for d in self.step_decisions)

    @property
    def requires_approval(self) -> bool:
        return any(
            d.decision == PolicyDecision.REQUIRE_APPROVAL for d in self.step_decisions
        )

    @property
    def blocked_steps(self) -> list[StepPolicyDecision]:
        return [d for d in self.step_decisions if d.decision == PolicyDecision.BLOCK]

    @property
    def approval_steps(self) -> list[StepPolicyDecision]:
        return [
            d
            for d in self.step_decisions
            if d.decision == PolicyDecision.REQUIRE_APPROVAL
        ]

    @property
    def allowed_steps(self) -> list[StepPolicyDecision]:
        return [d for d in self.step_decisions if d.decision == PolicyDecision.ALLOW]
