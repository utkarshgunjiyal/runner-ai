"""Optimizer output models.

The Optimizer turns a logical Plan (DAG) into an execution strategy — grouping
independent steps and annotating notes — without mutating the Plan.
See docs/architecture/v2.md §9.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models.plan import PlanStep


class OptimizationType(str, Enum):
    PARALLEL_GROUPING = "parallel_grouping"
    DUPLICATE_STEP_DETECTED = "duplicate_step_detected"
    BLOCKED_STEP_PRESERVED = "blocked_step_preserved"
    APPROVAL_STEP_MARKED = "approval_step_marked"
    NO_OP = "no_op"


class OptimizationNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: OptimizationType
    message: str
    step_ids: list[str] = Field(default_factory=list)


class ExecutionGroup(BaseModel):
    """A set of steps that may run together (parallel when it has >1 step)."""

    model_config = ConfigDict(frozen=True)

    group_id: str
    step_ids: list[str]
    parallel: bool


class OptimizedPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    original_plan_id: str
    steps: list[PlanStep]
    execution_groups: list[ExecutionGroup]


class OptimizationReport(BaseModel):
    notes: list[OptimizationNote] = Field(default_factory=list)

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def has_parallel_groups(self) -> bool:
        return any(n.type == OptimizationType.PARALLEL_GROUPING for n in self.notes)

    @property
    def parallel_group_count(self) -> int:
        return sum(
            1 for n in self.notes if n.type == OptimizationType.PARALLEL_GROUPING
        )
