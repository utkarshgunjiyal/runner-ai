"""Agent Run API (Phase 30).

POST /agent/run — the HTTP entry point to the V2 runtime:

    request → authenticate user → build_default_runtime() → ResumeCoordinator.start
            → API-safe AgentRunResponse

Completed runs return the answer; WAITING_* runs return a checkpoint id plus the
pending action/reason (the run is persisted for a future /agent/resume). The
internal RunContext and the full FinalPrompt are never exposed.

Config-free at import: dependencies (current user, resume coordinator) are
resolved at request time and overridable in tests. No streaming, no Mongo store,
no /agent/resume yet.
"""

from fastapi import APIRouter, Depends

from app.agent.checkpoint.store import InMemoryCheckpointStore, is_checkpointable
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.resume_coordinator import ResumeCoordinator
from app.schemas.agent import AgentRunRequest, AgentRunResponse

router = APIRouter(prefix="/agent", tags=["agent"])

# V1.5 has no real auth yet (routes use a dev user). Keep the same default here;
# a real ``get_current_user`` slots in via dependency override without touching
# the handler.
DEV_USER_ID = "dev_user"


def get_current_user() -> dict:
    """Current user dependency. Overridden by real auth (or a fake) when wired."""
    return {"user_id": DEV_USER_ID}


def resolve_user_id(user) -> str:
    """Robustly extract a user id from a dict or object (user_id / id / _id)."""
    if user is None:
        return DEV_USER_ID
    if isinstance(user, dict):
        for key in ("user_id", "id", "_id"):
            value = user.get(key)
            if value:
                return str(value)
        return DEV_USER_ID
    for attr in ("user_id", "id", "_id"):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    return DEV_USER_ID


# Lazily-built default coordinator (in-memory store for now — replaceable with a
# Mongo-backed store later without touching the handler).
_coordinator: ResumeCoordinator | None = None


def get_resume_coordinator() -> ResumeCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = ResumeCoordinator(build_default_runtime(), InMemoryCheckpointStore())
    return _coordinator


def _to_response(coord_result) -> AgentRunResponse:
    result = coord_result.result
    outcome = result.runtime_outcome
    # For waiting outcomes we return the checkpoint + pending fields, not an answer.
    answer = None if is_checkpointable(outcome) else result.answer.text
    return AgentRunResponse(
        run_id=result.run_id,
        thread_id=result.thread_id,
        runtime_outcome=outcome.value,
        answer=answer,
        checkpoint_id=coord_result.checkpoint_id,
        pending_action=result.pending_action,
        pending_reason=result.pending_reason,
        metadata={
            "behavior_path": result.behavior_path,
            "provider": result.answer.provider,
            "model": result.answer.model,
            "evaluation_passed": result.metadata.get("evaluation_passed"),
        },
    )


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(
    request: AgentRunRequest,
    user=Depends(get_current_user),
    coordinator: ResumeCoordinator = Depends(get_resume_coordinator),
) -> AgentRunResponse:
    user_id = resolve_user_id(user)
    coord_result = await coordinator.start(
        request.user_request,
        user_id,
        thread_id=request.thread_id,
        metadata=request.metadata,
    )
    return _to_response(coord_result)
