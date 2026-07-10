"""Structured Plan / DAG models — the schema the future Planner LLM emits.

Phase 3: models + validation + read-only helpers only. No planning, no
resolution of arg bindings, no scheduling, no execution.
See docs/architecture/v2.md §6.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PlanStepType(str, Enum):
    TOOL = "tool"
    FINAL_RESPONSE = "final_response"


class FinalResponseMode(str, Enum):
    ANSWER = "answer"
    SUMMARIZE_RESULTS = "summarize_results"
    ACTION_RESULT = "action_result"
    ASK_CLARIFICATION = "ask_clarification"


class PlanError(Exception):
    """Base error for plan helpers."""


class StepNotFoundError(PlanError):
    """Raised when a referenced step id does not exist in the plan."""


def _require_non_empty(value: str, field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class ArgBinding(BaseModel):
    """A reference to a previous step's output (not resolved in Phase 3).

    Example: ArgBinding(step_id="step_1", path="output.summary").
    """

    model_config = ConfigDict(frozen=True)

    step_id: str
    path: str

    @field_validator("step_id", "path")
    @classmethod
    def _non_empty(cls, value, info):
        return _require_non_empty(value, info.field_name)


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    step_type: PlanStepType
    capability_id: str | None = None
    description: str
    # args may hold literals or binding strings like "${step_1.output.summary}"
    # — kept as-is; bindings are not resolved in Phase 3.
    args: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    output_alias: str | None = None
    parallel_group: str | None = None

    @field_validator("id", "description")
    @classmethod
    def _non_empty(cls, value, info):
        return _require_non_empty(value, info.field_name)

    @field_validator("depends_on")
    @classmethod
    def _dedupe_preserve_order(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for dep in value:
            if dep not in seen:
                seen.append(dep)
        return seen

    @model_validator(mode="after")
    def _validate_step(self):
        if self.step_type == PlanStepType.TOOL and not (
            self.capability_id and self.capability_id.strip()
        ):
            raise ValueError("TOOL steps must have a non-empty capability_id")
        if self.id in self.depends_on:
            raise ValueError(f"step '{self.id}' cannot depend on itself")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    user_goal: str
    intent: str
    steps: list[PlanStep]
    final_response_mode: FinalResponseMode
    planner_notes: str | None = None

    @field_validator("id", "user_goal", "intent")
    @classmethod
    def _non_empty(cls, value, info):
        return _require_non_empty(value, info.field_name)

    @field_validator("steps")
    @classmethod
    def _at_least_one_step(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not value:
            raise ValueError("plan must contain at least one step")
        return value

    @model_validator(mode="after")
    def _validate_dag(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")

        position = {step_id: i for i, step_id in enumerate(ids)}
        id_set = set(ids)

        for i, step in enumerate(self.steps):
            for dep in step.depends_on:
                if dep not in id_set:
                    raise ValueError(
                        f"step '{step.id}' depends on unknown step '{dep}'"
                    )
                if position[dep] >= i:
                    raise ValueError(
                        f"step '{step.id}' depends on a later or same step '{dep}'"
                    )

        # Explicit cycle detection (belt-and-suspenders; the ordering rule above
        # already prevents cycles, but this keeps the invariant if ordering is
        # ever relaxed).
        if _has_cycle({step.id: list(step.depends_on) for step in self.steps}):
            raise ValueError("plan dependency graph contains a cycle")

        return self

    # -- Read-only helpers ---------------------------------------------------

    def get_step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise StepNotFoundError(f"Unknown step id: '{step_id}'")

    def dependency_graph(self) -> dict[str, list[str]]:
        """Map each step id to its dependency ids (in plan/declared order)."""
        return {step.id: list(step.depends_on) for step in self.steps}

    def root_steps(self) -> list[PlanStep]:
        """Steps with no dependencies (entry points)."""
        return [step for step in self.steps if not step.depends_on]

    def terminal_steps(self) -> list[PlanStep]:
        """Steps that nothing else depends on (leaves)."""
        depended_on: set[str] = set()
        for step in self.steps:
            depended_on.update(step.depends_on)
        return [step for step in self.steps if step.id not in depended_on]


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}

    def visit(node: str) -> bool:
        color[node] = GREY
        for neighbor in graph.get(node, []):
            if neighbor not in color:
                continue  # unknown deps handled elsewhere
            if color[neighbor] == GREY:
                return True
            if color[neighbor] == WHITE and visit(neighbor):
                return True
        color[node] = BLACK
        return False

    return any(color[node] == WHITE and visit(node) for node in graph)
