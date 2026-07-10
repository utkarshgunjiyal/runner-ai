"""Answer evaluation models (Phase 20).

The Answer Evaluation & Repair Engine judges a *draft* FinalAnswer before it is
returned. This module defines the data shapes only — deterministic checks live
in ``engine.py``. Provider-agnostic and config-free: pydantic only, no LLM, no
database, no application settings.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CheckSeverity(str, Enum):
    ERROR = "error"      # a failing ERROR check fails the whole evaluation
    WARNING = "warning"  # advisory; does not fail the evaluation
    INFO = "info"


class CheckResult(BaseModel):
    """One deterministic check outcome."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    severity: CheckSeverity = CheckSeverity.ERROR
    detail: str = ""


class RepairAction(str, Enum):
    NONE = "none"
    REGENERATE_WITH_SAME_CONTEXT = "regenerate_with_same_context"
    REGENERATE_WITH_STRONGER_INSTRUCTIONS = "regenerate_with_stronger_instructions"
    RETRIEVE_MORE_CONTEXT = "retrieve_more_context"
    RERUN_CAPABILITY = "rerun_capability"
    REPLAN = "replan"
    ASK_USER_FOR_CLARIFICATION = "ask_user_for_clarification"
    HUMAN_REVIEW = "human_review"
    RETURN_PARTIAL_WITH_WARNING = "return_partial_with_warning"
    FAIL_GRACEFULLY = "fail_gracefully"


class RepairDecision(BaseModel):
    """What to do about a failing (or passing) evaluation. Advisory only in this
    phase — the orchestrator does not yet act on it."""

    model_config = ConfigDict(frozen=True)

    action: RepairAction = RepairAction.NONE
    reason: str = ""
    max_attempts: int = 0
    target_stage: str | None = None
    metadata: dict = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    """The verdict for one draft answer."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    overall_score: float
    reason: str = ""
    missing_requirements: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    groundedness_score: float = 1.0
    completeness_score: float = 1.0
    citation_score: float = 1.0
    checks: list[CheckResult] = Field(default_factory=list)
    repair_decision: RepairDecision = Field(default_factory=RepairDecision)
    metadata: dict = Field(default_factory=dict)
