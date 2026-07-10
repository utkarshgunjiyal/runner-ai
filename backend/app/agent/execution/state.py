"""Shared Execution State — the per-run blackboard.

Holds every step's result and bucketed status lists. Mutable runtime object (not
a frozen model). See docs/architecture/v2.md §12.
"""

from app.agent.models.execution import StepExecutionResult, StepStatus


class ExecutionStateError(Exception):
    """Base error for execution-state access."""


class StepResultNotFoundError(ExecutionStateError):
    """Raised when a step result is requested but not present."""


_STATUS_BUCKET = {
    StepStatus.SUCCEEDED: "completed_steps",
    StepStatus.FAILED: "failed_steps",
    StepStatus.SKIPPED: "skipped_steps",
    StepStatus.BLOCKED: "blocked_steps",
    StepStatus.AWAITING_APPROVAL: "awaiting_approval_steps",
}


class ExecutionState:
    def __init__(self, run_id: str, plan_id: str) -> None:
        self.run_id = run_id
        self.plan_id = plan_id
        self.step_results: dict[str, StepExecutionResult] = {}
        self.completed_steps: list[str] = []
        self.failed_steps: list[str] = []
        self.skipped_steps: list[str] = []
        self.blocked_steps: list[str] = []
        self.awaiting_approval_steps: list[str] = []

    def record_result(self, result: StepExecutionResult) -> None:
        self.step_results[result.step_id] = result
        bucket = _STATUS_BUCKET.get(result.status)
        if bucket is not None:
            getattr(self, bucket).append(result.step_id)

    def get_result(self, step_id: str) -> StepExecutionResult:
        try:
            return self.step_results[step_id]
        except KeyError:
            raise StepResultNotFoundError(f"No result for step '{step_id}'") from None

    def has_result(self, step_id: str) -> bool:
        return step_id in self.step_results
