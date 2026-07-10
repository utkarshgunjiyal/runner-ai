"""Deterministic plan executor.

Runs an OptimizedPlan's execution groups in order, resolving arg bindings from
Shared Execution State and honoring policy decisions. Phase 7 is the runtime
foundation only:
  * groups run in order; steps within a group run SEQUENTIALLY (no real
    parallelism yet)
  * BLOCK / REQUIRE_APPROVAL steps are recorded but not executed (no HITL yet)
  * dependents of failed/blocked/skipped/awaiting steps are SKIPPED
  * no retries, no timeouts, no real tool adapters
"""

import re
import uuid
from datetime import datetime, timezone

from app.agent.execution.runner import ToolRunner
from app.agent.execution.state import ExecutionState
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.models.optimization import OptimizedPlan
from app.agent.models.plan import PlanStep
from app.agent.models.policy import PolicyDecision, PolicyReport

_BINDING_RE = re.compile(r"^\$\{([^}]*)\}$")

# Dependency statuses that force a dependent step to be skipped.
_BLOCKING_STATUSES = {
    StepStatus.FAILED,
    StepStatus.BLOCKED,
    StepStatus.SKIPPED,
    StepStatus.AWAITING_APPROVAL,
}


class BindingResolutionError(Exception):
    """Raised when an arg binding cannot be resolved from execution state."""


def _looks_like_binding(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("${")


def _resolve_binding(value: str, state: ExecutionState):
    match = _BINDING_RE.match(value.strip())
    if not match or "." not in match.group(1):
        raise BindingResolutionError(f"malformed binding: {value}")

    step_id, path = match.group(1).strip().split(".", 1)
    step_id, path = step_id.strip(), path.strip()
    if not path.startswith("output."):
        raise BindingResolutionError(f"binding path must start with 'output.': {value}")

    if not state.has_result(step_id):
        raise BindingResolutionError(f"binding references unknown step '{step_id}'")

    current = state.get_result(step_id).output
    parts = path[len("output."):].split(".")
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise BindingResolutionError(
                f"binding output field '{path}' missing on step '{step_id}'"
            )
        current = current[part]
    return current


def _resolve_args(args: dict, state: ExecutionState) -> dict:
    # Top-level bindings only in Phase 7; nested structures are passed as-is.
    resolved = {}
    for key, value in args.items():
        resolved[key] = _resolve_binding(value, state) if _looks_like_binding(value) else value
    return resolved


class PlanExecutor:
    def __init__(self, tool_runner: ToolRunner) -> None:
        self._runner = tool_runner

    def execute(
        self,
        optimized_plan: OptimizedPlan,
        policy_report: PolicyReport | None = None,
    ) -> ExecutionState:
        state = ExecutionState(
            run_id=uuid.uuid4().hex,
            plan_id=optimized_plan.original_plan_id,
        )
        policy_by_step = (
            {d.step_id: d for d in policy_report.step_decisions}
            if policy_report is not None
            else {}
        )
        steps_by_id = {step.id: step for step in optimized_plan.steps}

        for group in optimized_plan.execution_groups:
            for step_id in group.step_ids:
                self._execute_step(steps_by_id[step_id], state, policy_by_step)

        return state

    def _execute_step(self, step: PlanStep, state: ExecutionState, policy_by_step: dict) -> None:
        decision = policy_by_step.get(step.id)

        if decision is not None and decision.decision == PolicyDecision.BLOCK:
            self._record(state, step, StepStatus.BLOCKED, error="blocked by policy")
            return

        if decision is not None and decision.decision == PolicyDecision.REQUIRE_APPROVAL:
            self._record(state, step, StepStatus.AWAITING_APPROVAL)
            return

        blocker = self._blocking_dependency(step, state)
        if blocker is not None:
            self._record(
                state, step, StepStatus.SKIPPED,
                error=f"dependency '{blocker}' did not succeed",
            )
            return

        try:
            resolved_args = _resolve_args(step.args, state)
        except BindingResolutionError as exc:
            self._record(state, step, StepStatus.FAILED, error=str(exc))
            return

        started = datetime.now(timezone.utc)
        try:
            output = self._runner.run(step, resolved_args)
        except Exception as exc:  # noqa: BLE001 - record any tool failure
            ended = datetime.now(timezone.utc)
            self._record(
                state, step, StepStatus.FAILED,
                input=resolved_args, error=str(exc),
                started_at=started, ended_at=ended,
            )
            return

        ended = datetime.now(timezone.utc)
        self._record(
            state, step, StepStatus.SUCCEEDED,
            input=resolved_args, output=output,
            started_at=started, ended_at=ended,
        )

    @staticmethod
    def _blocking_dependency(step: PlanStep, state: ExecutionState) -> str | None:
        for dep in step.depends_on:
            if not state.has_result(dep):
                return dep
            if state.get_result(dep).status in _BLOCKING_STATUSES:
                return dep
        return None

    def _record(
        self,
        state: ExecutionState,
        step: PlanStep,
        status: StepStatus,
        *,
        input: dict | None = None,
        output: dict | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        duration_ms = None
        if started_at is not None and ended_at is not None:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)

        state.record_result(
            StepExecutionResult(
                step_id=step.id,
                capability_id=step.capability_id,
                status=status,
                input=input or {},
                output=output or {},
                error=error,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
            )
        )
