"""Resume Runtime (Phase 25).

Loads a checkpoint, rehydrates its RunContext, records the caller's resolution
(an approval, a clarification, newly-available context, …) on the RunContext, and
marks the checkpoint resumed — handing back a RunContext ready for a *future*
orchestrator continuation.

Data-layer only: it does NOT re-run the orchestrator and does NOT execute the
deferred stage. Config-free: no LLM, no database, no application settings.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.checkpoint.rehydrate import rehydrate_run_context
from app.agent.checkpoint.store import CheckpointStore
from app.agent.runtime.context import RunContext


class ResumeKind(str, Enum):
    APPROVAL = "approval"
    REJECTION = "rejection"
    CLARIFICATION = "clarification"
    CONTEXT_AVAILABLE = "context_available"
    REPLAN_REQUESTED = "replan_requested"


class ResumeResolution(BaseModel):
    """The caller's answer to whatever the run was waiting for."""

    model_config = ConfigDict(frozen=True)

    kind: ResumeKind
    value: Any = None
    reason: str = ""
    metadata: dict = Field(default_factory=dict)


class ResumeRuntime:
    def resume(
        self,
        store: CheckpointStore,
        checkpoint_id: str,
        resolution: ResumeResolution,
    ) -> RunContext:
        # Missing checkpoint → CheckpointNotFoundError propagates.
        record = store.load(checkpoint_id)

        run_context = rehydrate_run_context(record.run_context_snapshot)
        run_context.metadata["resume"] = {
            "kind": resolution.kind.value,
            "value": resolution.value,
            "reason": resolution.reason,
            "metadata": dict(resolution.metadata),
            "checkpoint_id": checkpoint_id,
            "pending_action": record.pending_action,
            "runtime_outcome": record.runtime_outcome.value,
        }

        store.mark_resumed(checkpoint_id)
        return run_context
