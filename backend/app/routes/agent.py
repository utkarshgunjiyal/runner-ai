"""Agent Run + Resume API (Phase 30-31).

POST /agent/run — the HTTP entry point to the V2 runtime:

    request → authenticate user → build_default_runtime() → ResumeCoordinator.start
            → API-safe AgentRunResponse

POST /agent/resume — continue a paused run (Phase 31): map the caller's
resolution to a domain ResumeResolution and drive ResumeCoordinator.resume over
the *same* in-memory checkpoint store. An unknown checkpoint id is a 404.

Completed runs return the answer; WAITING_* runs return a checkpoint id plus the
pending action/reason (the run is persisted for a later /agent/resume). The
internal RunContext and the full FinalPrompt are never exposed.

Config-free at import: dependencies (current user, resume coordinator) are
resolved at request time and overridable in tests. No streaming, no Mongo store.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.checkpoint.resume import ResumeResolution
from app.agent.checkpoint.store import (
    CheckpointNotFoundError,
    InMemoryCheckpointStore,
    is_checkpointable,
)
from app.agent.runtime.events import RuntimeEvent
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.resume_coordinator import ResumeCoordinator
from app.agent.runtime.streaming import RuntimeStreamer
from app.schemas.agent import AgentResumeRequest, AgentRunRequest, AgentRunResponse

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


# Lazily-built, shared default runtime. /agent/run, /agent/resume and
# /agent/run/stream all use the SAME orchestrator instance — no duplicated
# runtime construction. The in-memory store is replaceable with a Mongo-backed
# store later without touching any handler.
_orchestrator = None
_coordinator: ResumeCoordinator | None = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = build_default_runtime()
    return _orchestrator


def get_resume_coordinator() -> ResumeCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = ResumeCoordinator(_get_orchestrator(), InMemoryCheckpointStore())
    return _coordinator


def get_runtime_streamer() -> RuntimeStreamer:
    # Wraps the same orchestrator used by /agent/run — transport only.
    return RuntimeStreamer(_get_orchestrator())


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


@router.post("/resume", response_model=AgentRunResponse)
async def resume_agent(
    request: AgentResumeRequest,
    coordinator: ResumeCoordinator = Depends(get_resume_coordinator),
) -> AgentRunResponse:
    resolution = ResumeResolution(
        kind=request.resolution.kind,
        value=request.resolution.value,
        reason=request.resolution.reason,
        metadata=request.resolution.metadata,
    )
    try:
        coord_result = await coordinator.resume(request.checkpoint_id, resolution)
    except CheckpointNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_response(coord_result)


def _sse(event: RuntimeEvent) -> str:
    """Serialize one RuntimeEvent as an SSE frame (event: <type>\\ndata: <json>)."""
    return f"event: {event.type.value}\ndata: {json.dumps(event.model_dump())}\n\n"


@router.post("/run/stream")
async def run_agent_stream(
    request: AgentRunRequest,
    user=Depends(get_current_user),
    streamer: RuntimeStreamer = Depends(get_runtime_streamer),
) -> StreamingResponse:
    user_id = resolve_user_id(user)

    async def event_source():
        # Transport only: the RuntimeStreamer owns event ordering/generation; the
        # route just serializes each RuntimeEvent to the SSE wire format.
        async for event in streamer.run_stream(
            request.user_request, user_id,
            thread_id=request.thread_id, metadata=request.metadata,
        ):
            yield _sse(event)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
