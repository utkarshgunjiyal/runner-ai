"""Resume Coordinator (Phase 27).

Ties the already-built pieces into one in-memory pause/resume loop:

    start()  → orchestrator.run() → if WAITING_* : checkpoint_store.save() → id
    resume() → ResumeRuntime.resume() → orchestrator.continue_run()
                                      → if still WAITING_* : save() → new id

Separation of concerns is strict: the coordinator owns *checkpointing decisions*,
the orchestrator owns runtime execution, ResumeRuntime owns rehydration, and the
CheckpointStore owns persistence. Only WAITING_* outcomes are checkpointed.

Config-free: no LLM, no database, no application settings, no endpoints.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.agent.checkpoint.resume import ResumeResolution, ResumeRuntime
from app.agent.checkpoint.store import CheckpointStore, is_checkpointable
from app.agent.runtime.orchestrator import AgentRunResult


class ResumeCoordinatorResult(BaseModel):
    """What the coordinator returns from start()/resume()."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: AgentRunResult
    checkpoint_id: str | None = None          # created when this result is WAITING_*
    resumed_checkpoint_id: str | None = None  # the checkpoint resume() consumed
    metadata: dict = Field(default_factory=dict)


class ResumeCoordinator:
    def __init__(self, orchestrator, checkpoint_store: CheckpointStore, resume_runtime=None) -> None:
        self._orchestrator = orchestrator
        self._store = checkpoint_store
        self._resume_runtime = resume_runtime or ResumeRuntime()

    async def start(
        self,
        user_request: str,
        user_id: str,
        thread_id: str | None = None,
        metadata: dict | None = None,
    ) -> ResumeCoordinatorResult:
        result = await self._orchestrator.run(
            user_request, user_id, thread_id=thread_id, metadata=metadata
        )
        checkpoint_id = self._maybe_checkpoint(result)
        return ResumeCoordinatorResult(
            result=result,
            checkpoint_id=checkpoint_id,
            metadata=self._summary(result, checkpoint_id),
        )

    async def resume(
        self, checkpoint_id: str, resolution: ResumeResolution
    ) -> ResumeCoordinatorResult:
        # ResumeRuntime rehydrates, injects the resolution, and marks resumed.
        run_context = self._resume_runtime.resume(self._store, checkpoint_id, resolution)
        result = await self._orchestrator.continue_run(run_context)
        new_checkpoint_id = self._maybe_checkpoint(result)
        return ResumeCoordinatorResult(
            result=result,
            checkpoint_id=new_checkpoint_id,
            resumed_checkpoint_id=checkpoint_id,
            metadata=self._summary(result, new_checkpoint_id),
        )

    # -- Internals -----------------------------------------------------------

    def _maybe_checkpoint(self, result: AgentRunResult) -> str | None:
        """Checkpoint only WAITING_* outcomes; preserve pending action/reason."""
        if not is_checkpointable(result.runtime_outcome):
            return None
        record = self._store.save(
            result.run_context,
            result.runtime_outcome,
            pending_action=result.pending_action,
            pending_reason=result.pending_reason,
        )
        return record.checkpoint_id

    @staticmethod
    def _summary(result: AgentRunResult, checkpoint_id: str | None) -> dict:
        return {
            "runtime_outcome": result.runtime_outcome.value,
            "pending_action": result.pending_action,
            "pending_reason": result.pending_reason,
            "checkpointed": checkpoint_id is not None,
        }
