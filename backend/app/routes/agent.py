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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.checkpoint.resume import ResumeResolution
from app.sse import sse_event_source
from app.agent.checkpoint.store import (
    CheckpointConflictError,
    CheckpointNotFoundError,
    InMemoryCheckpointStore,
    is_checkpointable,
)
from app.agent.runtime.events import RuntimeEvent
from app.agent.runtime.factory import build_default_runtime
from app.agent.runtime.outcome import RuntimeOutcome
from app.agent.runtime.resume_coordinator import AsyncResumeCoordinator
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


# Lazily-built, shared default runtime + checkpoint store. /agent/run,
# /agent/resume and /agent/run/stream all use the SAME orchestrator + coordinator
# + store instance — no duplicated construction, no store-per-request. The store
# defaults to InMemory (config-free); production swaps in a Mongo-backed store
# via ``configure_checkpoint_store`` at startup, without touching any handler.
_orchestrator = None
_checkpoint_store = None
_coordinator: AsyncResumeCoordinator | None = None
_use_real_llm = False
_mcp_registry_manager = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = build_default_runtime(
            use_real_llm=_use_real_llm, mcp_registry_manager=_mcp_registry_manager
        )
    return _orchestrator


def configure_agent_runtime(*, use_real_llm: bool = False, mcp_registry_manager=None) -> None:
    """Composition-root hook: select the LLM provider mode (and optionally a
    pre-discovered MCP registry manager) before the shared orchestrator is first
    built. Providers/capabilities are built once and shared across /agent/run,
    /agent/resume and /agent/run/stream. Routes never read config; the MCP manager
    is composed and its transport lifecycle owned by the composition root."""
    global _use_real_llm, _mcp_registry_manager, _orchestrator, _coordinator
    _use_real_llm = bool(use_real_llm)
    _mcp_registry_manager = mcp_registry_manager
    _orchestrator = None  # rebuild with the selected providers/capabilities on next use
    _coordinator = None


def get_checkpoint_store():
    global _checkpoint_store
    if _checkpoint_store is None:
        _checkpoint_store = InMemoryCheckpointStore()
    return _checkpoint_store


def configure_checkpoint_store(store) -> None:
    """Composition-root hook: install the checkpoint store (e.g. a
    MongoCheckpointStore) before the coordinator is first built. Call once at
    startup. Resets the shared coordinator so it rebuilds against the new store.
    Persistence logic stays out of the routes — this only *selects* the store."""
    global _checkpoint_store, _coordinator
    _checkpoint_store = store
    _coordinator = None


def get_resume_coordinator() -> AsyncResumeCoordinator:
    # Async-safe coordinator: synchronous checkpoint I/O is offloaded off the
    # event loop. Shared singleton — one coordinator + store per process.
    global _coordinator
    if _coordinator is None:
        _coordinator = AsyncResumeCoordinator(_get_orchestrator(), get_checkpoint_store())
    return _coordinator


def get_runtime_streamer() -> RuntimeStreamer:
    # Wraps the same orchestrator used by /agent/run — transport only. The
    # checkpointer is the shared coordinator's persistence step, so a streamed
    # WAITING_* run is checkpointed in the SAME store /agent/resume reads and the
    # terminal event carries a resumable checkpoint_id (Phase 41B).
    return RuntimeStreamer(
        _get_orchestrator(), checkpointer=get_resume_coordinator().checkpoint_result
    )


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
        metadata=_safe_response_metadata(result),
    )


def _safe_response_metadata(result) -> dict:
    metadata = {
        "behavior_path": result.behavior_path,
        "provider": result.answer.provider,
        "model": result.answer.model,
        "evaluation_passed": result.metadata.get("evaluation_passed"),
    }
    # Surface API-safe provider-failure classification when present (no vendor
    # detail — only stage/code/retryable/clarification flags).
    for key in ("failure_stage", "error_code", "retryable", "clarification_needed",
                "planner_error_type"):
        if result.metadata.get(key) is not None:
            metadata[key] = result.metadata[key]
    return metadata


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(
    request: AgentRunRequest,
    user=Depends(get_current_user),
    coordinator=Depends(get_resume_coordinator),
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
    coordinator=Depends(get_resume_coordinator),
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
    except CheckpointConflictError as exc:
        # Already resumed / cancelled / not active — or a concurrent second
        # resume that lost the atomic claim.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(coord_result)


# SSE keep-alive interval (seconds). Set by the composition root; 0 disables
# heartbeats. Kept as a module global so routes stay config-free at import.
_sse_heartbeat_seconds: float = 15.0


def configure_sse(*, heartbeat_seconds: float) -> None:
    """Composition-root hook: set the SSE heartbeat interval (Phase 42A)."""
    global _sse_heartbeat_seconds
    _sse_heartbeat_seconds = float(heartbeat_seconds)


def _sse(event: RuntimeEvent) -> str:
    """Serialize one RuntimeEvent as an SSE frame (event: <type>\\ndata: <json>)."""
    return f"event: {event.type.value}\ndata: {json.dumps(event.model_dump())}\n\n"


@router.post("/run/stream")
async def run_agent_stream(
    request: AgentRunRequest,
    http_request: Request,
    user=Depends(get_current_user),
    streamer: RuntimeStreamer = Depends(get_runtime_streamer),
) -> StreamingResponse:
    user_id = resolve_user_id(user)

    # Transport only: the RuntimeStreamer owns event ordering/generation; the SSE
    # helper adds heartbeats and cancels the run cleanly on client disconnect.
    event_source = sse_event_source(
        streamer.run_stream(
            request.user_request, user_id,
            thread_id=request.thread_id, metadata=request.metadata,
        ),
        serialize=_sse,
        is_disconnected=http_request.is_disconnected,
        heartbeat_seconds=_sse_heartbeat_seconds,
    )

    return StreamingResponse(
        event_source,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
