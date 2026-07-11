"""Planner Runtime (Phase 15).

Planner Runtime is an *orchestrator*, not a second execution engine. It walks an
ExecutionPlan's tasks sequentially and runs each one through the Phase 14
DirectRuntime — the only execution engine. Direct Runtime owns execution;
Planner Runtime owns sequencing, ExecutionState, stop/continue policy, and
result aggregation onto the RunContext.

    RunContext → Planner (reasoning done elsewhere) → ExecutionPlan
      → for each task: RunContext-aware Capability Retrieval → DirectRuntime.run
      → merge outputs/evidence, update ExecutionState → repeat until done.

Planner reasoning (turning a goal into tasks) is NOT implemented here — the
ExecutionPlan is received as input. Recovery is deterministic only (delegated to
DirectRuntime); the Reflection LLM is never invoked. Deterministic and
config-free: no LLM, no database, no application settings.
"""

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.capabilities.retriever import CapabilityRetriever
from app.agent.models.execution import StepExecutionResult, StepStatus
from app.agent.runtime import diagnostics
from app.agent.runtime.context import BehaviorPath, BehaviorProfile, RunContext
from app.agent.runtime.direct_runtime import ExecutionStatus as DirectStatus


class PlannerRuntimeError(Exception):
    """Base error for the Planner Runtime."""


class NotPlannerPathError(PlannerRuntimeError):
    """Raised when run() is called on a RunContext not routed to PLANNER."""


class RuntimeStatus(str, Enum):
    COMPLETED = "completed"
    STOPPED_REQUIRED_FAILURE = "stopped_required_failure"
    STOPPED_POLICY_BLOCK = "stopped_policy_block"
    STOPPED_AWAITING_APPROVAL = "stopped_awaiting_approval"


class PlannerTask(BaseModel):
    """One unit of orchestration — handed to DirectRuntime as its request.

    ``optional`` distinguishes a task whose failure is tolerated (continue) from
    a required task whose failure stops the plan.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    request: str
    optional: bool = False
    metadata: dict = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    """The received plan (produced by planner reasoning that lives elsewhere)."""

    model_config = ConfigDict(frozen=True)

    id: str
    goal: str
    tasks: list[PlannerTask] = Field(default_factory=list)


class DirectRuntimeLike(Protocol):
    async def run(self, run_context: RunContext) -> RunContext:
        ...


class PlannerRuntime:
    def __init__(
        self,
        direct_runtime: DirectRuntimeLike,
        retriever: CapabilityRetriever,
        *,
        top_k: int = 5,
    ) -> None:
        self._direct = direct_runtime
        self._retriever = retriever
        self._top_k = top_k

    async def run(self, run_context: RunContext, plan: ExecutionPlan) -> RunContext:
        # 1. Only the PLANNER path is handled here.
        path = self._resolve_path(run_context)
        if path != BehaviorPath.PLANNER:
            raise NotPlannerPathError(
                f"PlannerRuntime requires PLANNER path, got '{path.value}'"
            )

        state = run_context.execution_state
        pending = [task.id for task in plan.tasks]
        completed: list[str] = []
        failed: list[str] = []
        partial: list[str] = []
        order: list[str] = []
        status = RuntimeStatus.COMPLETED

        # 3-4. Iterate tasks sequentially.
        for task in plan.tasks:
            pending.remove(task.id)
            self._write_progress(
                run_context, current=task.id, completed=completed, failed=failed,
                partial=partial, pending=pending, order=order, status=status,
            )

            # 4a. RunContext-aware capability retrieval (scoping + telemetry).
            task_context = self._build_task_context(run_context, task)
            candidates = [
                match.tool.id
                for match in self._retriever.retrieve_for_run_context(
                    task_context, top_k=self._top_k
                ).matches
            ]
            task_context.metadata["capability_candidates"] = candidates

            # 4b. Execute through DirectRuntime — the only execution engine.
            task_context = await self._direct.run(task_context)

            # Diagnostics (Phase 46.2.3): how this plan task resolved to a tool —
            # reveals whether the planner requested one capability but the task
            # resolved/executed a different one. Emitted onto the PARENT context so
            # every diagnostic for the request is traceable together.
            resolved = (list(task_context.selected_capabilities) or [None])[0]
            resolved_tool = task_context.tool_outputs[-1] if task_context.tool_outputs else None
            diagnostics.emit(
                run_context, "agent.plan_tool_resolved",
                task_id=task.id,
                planner_candidates=candidates[:8],
                resolved_capability=resolved,
                executed_capability=getattr(resolved_tool, "capability_id", None),
            )

            # 5. Merge results and update ExecutionState.
            self._merge(run_context, task, task_context, candidates)
            step_status, stop_reason = self._classify(task, task_context)
            state.record_result(
                StepExecutionResult(
                    step_id=task.id,
                    capability_id=(task_context.selected_capabilities or [None])[0],
                    status=step_status,
                    output=self._last_output(task_context),
                    error=task_context.metadata.get("direct_runtime", {}).get("error_code"),
                )
            )
            order.append(task.id)
            if step_status == StepStatus.SUCCEEDED:
                completed.append(task.id)
                if task_context.metadata.get("execution_status") == DirectStatus.PARTIAL.value:
                    partial.append(task.id)
            else:
                failed.append(task.id)

            # 6-7. Stop on required failure / policy block / approval; else continue.
            if stop_reason is not None:
                status = stop_reason
                break

        self._write_progress(
            run_context, current=None, completed=completed, failed=failed,
            partial=partial, pending=pending, order=order, status=status,
        )
        return run_context

    # -- Internals -----------------------------------------------------------

    def _resolve_path(self, run_context: RunContext) -> BehaviorPath:
        if run_context.behavior_profile is not None:
            return run_context.behavior_profile.path
        decision = run_context.metadata.get("behavior_decision")
        if isinstance(decision, dict) and "path" in decision:
            return BehaviorPath(decision["path"])
        raise PlannerRuntimeError(
            "RunContext has no behavior decision; run the Behavior Gate first"
        )

    def _build_task_context(self, parent: RunContext, task: PlannerTask) -> RunContext:
        # Fresh DIRECT context per task. The parent's working context is copied
        # (frozen items), so the preserved working context is never mutated.
        inherited_args = parent.metadata.get("capability_args")
        metadata = dict(task.metadata)
        if inherited_args is not None and "capability_args" not in metadata:
            metadata["capability_args"] = inherited_args

        task_context = RunContext.create(
            user_request=task.request,
            user_id=parent.user_id,
            thread_id=parent.thread_id,
            working_context=parent.working_context,
            metadata=metadata,
        )
        task_context.attach_behavior_profile(
            BehaviorProfile(
                path=BehaviorPath.DIRECT,
                reason=f"planner task {task.id}",
                confidence=1.0,
            )
        )
        return task_context

    def _merge(
        self,
        parent: RunContext,
        task: PlannerTask,
        task_context: RunContext,
        candidates: list[str],
    ) -> None:
        # task_context started empty, so everything on it is this task's output.
        for output in task_context.tool_outputs:
            parent.append_tool_output(output)
        for item in task_context.evidence:
            parent.append_evidence(item)

        recovery = task_context.metadata.get("recovery_events") or []
        if recovery:
            parent_recovery = parent.metadata.setdefault("recovery_events", [])
            parent_recovery.extend({"task_id": task.id, **event} for event in recovery)

        history = parent.metadata.setdefault("execution_history", [])
        history.append(
            {
                "task_id": task.id,
                "request": task.request,
                "optional": task.optional,
                "capability_candidates": candidates,
                "selected_capabilities": list(task_context.selected_capabilities),
                "execution_status": task_context.metadata.get("execution_status"),
                "recovery_events": recovery,
            }
        )

    def _classify(
        self, task: PlannerTask, task_context: RunContext
    ) -> tuple[StepStatus, RuntimeStatus | None]:
        metadata = task_context.metadata
        # Explicit control signals (forward-compatible with Policy/HITL wiring).
        if metadata.get("policy_block"):
            return StepStatus.BLOCKED, RuntimeStatus.STOPPED_POLICY_BLOCK
        if metadata.get("requires_approval"):
            return StepStatus.AWAITING_APPROVAL, RuntimeStatus.STOPPED_AWAITING_APPROVAL

        status = metadata.get("execution_status")
        if status in (DirectStatus.SUCCESS.value, DirectStatus.PARTIAL.value):
            return StepStatus.SUCCEEDED, None

        # Failure / needs-user. Optional tasks continue; required tasks stop.
        if task.optional:
            return StepStatus.FAILED, None
        return StepStatus.FAILED, RuntimeStatus.STOPPED_REQUIRED_FAILURE

    @staticmethod
    def _last_output(task_context: RunContext) -> dict:
        if task_context.tool_outputs:
            return dict(task_context.tool_outputs[-1].output)
        return {}

    @staticmethod
    def _write_progress(
        run_context: RunContext,
        *,
        current,
        completed,
        failed,
        partial,
        pending,
        order,
        status,
    ) -> None:
        run_context.metadata["planner_runtime"] = {
            "current_task": current,
            "completed_tasks": list(completed),
            "failed_tasks": list(failed),
            "partial_tasks": list(partial),
            "pending_tasks": list(pending),
            "execution_order": list(order),
            "runtime_status": status.value,
        }
