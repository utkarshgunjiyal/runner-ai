"""RunContext rehydration (Phase 25).

The inverse of ``snapshot_run_context`` (Phase 24): rebuild a live ``RunContext``
from a ``CheckpointRecord.run_context_snapshot`` so a waiting run can continue.

Data-layer only — this reconstructs state, it does not re-run the orchestrator or
execute anything. The input snapshot is deep-copied first, so the stored
checkpoint is never aliased or mutated. Config-free: no LLM, no database, no
application settings.
"""

import copy

from app.agent.execution.state import ExecutionState
from app.agent.models.execution import StepExecutionResult
from app.agent.models.plan import Plan
from app.agent.runtime.context import (
    BehaviorProfile,
    EvidenceItem,
    RunContext,
    ToolOutput,
    WorkingContextItem,
)

_BUCKETS = (
    "completed_steps",
    "failed_steps",
    "skipped_steps",
    "blocked_steps",
    "awaiting_approval_steps",
)


def _rehydrate_execution_state(data: dict, fallback_run_id: str) -> ExecutionState:
    state = ExecutionState(
        run_id=data.get("run_id", fallback_run_id),
        plan_id=data.get("plan_id", ""),
    )
    for bucket in _BUCKETS:
        setattr(state, bucket, list(data.get(bucket, [])))
    state.step_results = {
        step_id: StepExecutionResult(**result)
        for step_id, result in (data.get("step_results") or {}).items()
    }
    return state


def rehydrate_run_context(snapshot: dict) -> RunContext:
    """Rebuild a RunContext from a checkpoint snapshot (non-mutating)."""

    snapshot = copy.deepcopy(snapshot)

    working_context = [
        WorkingContextItem(**item) for item in snapshot.get("working_context", [])
    ]
    execution_state = _rehydrate_execution_state(
        snapshot.get("execution_state") or {}, snapshot["run_id"]
    )

    run_context = RunContext(
        user_request=snapshot["user_request"],
        user_id=snapshot["user_id"],
        run_id=snapshot["run_id"],
        thread_id=snapshot.get("thread_id"),
        working_context=working_context,
        execution_state=execution_state,
        metadata=snapshot.get("metadata") or {},
    )

    behavior_profile = snapshot.get("behavior_profile")
    if behavior_profile is not None:
        run_context.behavior_profile = BehaviorProfile(**behavior_profile)

    run_context.selected_capabilities = list(snapshot.get("selected_capabilities", []))

    plan = snapshot.get("plan")
    if plan is not None:
        run_context.plan = Plan(**plan)

    run_context.tool_outputs = [ToolOutput(**o) for o in snapshot.get("tool_outputs", [])]
    run_context.evidence = [EvidenceItem(**e) for e in snapshot.get("evidence", [])]

    return run_context
