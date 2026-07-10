"""Checkpoint Store (Phase 24).

Persists a waiting run's RunContext so it can be resumed later. This phase ships
the store *contract* plus an in-memory implementation — no MongoDB, no resume
endpoint, no HITL UI yet.

    RuntimeOutcome == WAITING_*  →  store.save(...)  →  checkpoint_id
                                                     →  (later) load + resume

Snapshotting reads copies of the RunContext (never mutates it) and produces a
deterministic, JSON-safe dict. Only WAITING_* outcomes are checkpointable; a
terminal outcome (completed/failed) is rejected. Config-free: no LLM, no
database driver, no application settings.
"""

import copy
import uuid
from datetime import datetime, timezone
from typing import Protocol

from app.agent.checkpoint.models import CheckpointRecord, CheckpointStatus
from app.agent.runtime.context import RunContext
from app.agent.runtime.outcome import RuntimeOutcome

WAITING_OUTCOMES = frozenset(
    {
        RuntimeOutcome.WAITING_FOR_CONTEXT,
        RuntimeOutcome.WAITING_FOR_USER,
        RuntimeOutcome.WAITING_FOR_APPROVAL,
        RuntimeOutcome.WAITING_FOR_REPLAN,
    }
)


class CheckpointError(Exception):
    """Base error for the checkpoint store."""


class CheckpointNotFoundError(CheckpointError):
    """Raised when a checkpoint_id is not present in the store."""


class NonCheckpointableOutcomeError(CheckpointError):
    """Raised when trying to checkpoint a non-WAITING (terminal) outcome."""


def is_checkpointable(outcome: RuntimeOutcome) -> bool:
    return outcome in WAITING_OUTCOMES


def snapshot_run_context(run_context: RunContext) -> dict:
    """Serialize a RunContext into a JSON-safe dict without mutating it."""

    state = run_context.execution_state
    return {
        "run_id": run_context.run_id,
        "user_id": run_context.user_id,
        "thread_id": run_context.thread_id,
        "user_request": run_context.user_request,
        # working_context returns a copy of frozen items — originals untouched.
        "working_context": [item.model_dump() for item in run_context.working_context],
        "selected_capabilities": list(run_context.selected_capabilities),
        "behavior_profile": (
            run_context.behavior_profile.model_dump()
            if run_context.behavior_profile is not None
            else None
        ),
        "plan": run_context.plan.model_dump() if run_context.plan is not None else None,
        "tool_outputs": [o.model_dump() for o in run_context.tool_outputs],
        "evidence": [e.model_dump() for e in run_context.evidence],
        "execution_state": {
            "run_id": state.run_id,
            "plan_id": state.plan_id,
            "completed_steps": list(state.completed_steps),
            "failed_steps": list(state.failed_steps),
            "skipped_steps": list(state.skipped_steps),
            "blocked_steps": list(state.blocked_steps),
            "awaiting_approval_steps": list(state.awaiting_approval_steps),
            "step_results": {
                step_id: result.model_dump()
                for step_id, result in state.step_results.items()
            },
        },
        # deepcopy so the snapshot never aliases (or mutates) live metadata.
        "metadata": copy.deepcopy(run_context.metadata),
    }


class CheckpointStore(Protocol):
    def save(
        self, run_context: RunContext, runtime_outcome: RuntimeOutcome,
        pending_action: str | None = None, pending_reason: str | None = None,
        metadata: dict | None = None,
    ) -> CheckpointRecord: ...

    def load(self, checkpoint_id: str) -> CheckpointRecord: ...

    def mark_resumed(self, checkpoint_id: str) -> CheckpointRecord: ...

    def cancel(self, checkpoint_id: str, reason: str | None = None) -> CheckpointRecord: ...


class InMemoryCheckpointStore:
    """In-memory CheckpointStore for tests and local runtime.

    ``clock`` and ``id_factory`` are injectable for deterministic tests.
    """

    def __init__(self, *, clock=None, id_factory=None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._records: dict[str, CheckpointRecord] = {}

    def save(
        self,
        run_context: RunContext,
        runtime_outcome: RuntimeOutcome,
        pending_action: str | None = None,
        pending_reason: str | None = None,
        metadata: dict | None = None,
    ) -> CheckpointRecord:
        if not is_checkpointable(runtime_outcome):
            raise NonCheckpointableOutcomeError(
                f"outcome '{runtime_outcome.value}' is terminal and not checkpointable"
            )
        now = self._clock()
        record = CheckpointRecord(
            checkpoint_id=self._id_factory(),
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            runtime_outcome=runtime_outcome,
            pending_action=pending_action,
            pending_reason=pending_reason,
            run_context_snapshot=snapshot_run_context(run_context),
            status=CheckpointStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        self._records[record.checkpoint_id] = record
        return record

    def load(self, checkpoint_id: str) -> CheckpointRecord:
        try:
            return self._records[checkpoint_id]
        except KeyError:
            raise CheckpointNotFoundError(f"no checkpoint '{checkpoint_id}'") from None

    def mark_resumed(self, checkpoint_id: str) -> CheckpointRecord:
        return self._transition(checkpoint_id, CheckpointStatus.RESUMED)

    def cancel(self, checkpoint_id: str, reason: str | None = None) -> CheckpointRecord:
        record = self.load(checkpoint_id)
        metadata = dict(record.metadata)
        if reason is not None:
            metadata["cancel_reason"] = reason
        return self._replace(record, status=CheckpointStatus.CANCELLED, metadata=metadata)

    # -- Internals -----------------------------------------------------------

    def _transition(self, checkpoint_id: str, status: CheckpointStatus) -> CheckpointRecord:
        return self._replace(self.load(checkpoint_id), status=status)

    def _replace(self, record: CheckpointRecord, **updates) -> CheckpointRecord:
        updates.setdefault("updated_at", self._clock())
        updated = record.model_copy(update=updates)
        self._records[updated.checkpoint_id] = updated
        return updated
