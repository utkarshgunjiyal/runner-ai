"""Runtime streaming (Phase 32; Phase 38 true token streaming).

Exposes a runtime execution as an async stream of ``RuntimeEvent``s, without
changing any runtime decision, planning, or retrieval. ``RuntimeStreamer`` wraps
an injected orchestrator and adds ``run_stream()`` alongside the unchanged
``run()``.

Phase 38. Streaming is now *live*: the streamer runs ``orchestrator.run`` with a
``stream_sink`` and drains the events off a queue as the pipeline produces them.
Answer chunks are emitted as the provider yields them — not reconstructed after
the answer already exists. The streamer owns only the envelope: it emits
``runtime_started`` up front and the single terminal event
(``runtime_completed`` on success, ``runtime_failed`` on a raised error or a
provider-failure outcome) after ``run`` returns. Everything in between — context,
retrieval, planner, tools, answer_started/chunk/completed, evaluation, repair —
is emitted by the orchestrator in true pipeline order.

Config-free and fully injectable: no LLM, no database, no settings. Never
inspects planner/evaluation/repair internals beyond the API-safe metadata the
runtime already recorded.
"""

import asyncio
from collections.abc import AsyncIterator

from app.agent.runtime.events import RuntimeEvent, RuntimeEventType as E
from app.agent.runtime.outcome import RuntimeOutcome

_SENTINEL = object()

# Waiting outcomes are the resumable ones (checkpointed for /agent/resume).
_WAITING_OUTCOMES = {
    RuntimeOutcome.WAITING_FOR_CONTEXT,
    RuntimeOutcome.WAITING_FOR_USER,
    RuntimeOutcome.WAITING_FOR_APPROVAL,
    RuntimeOutcome.WAITING_FOR_REPLAN,
}


class _Sequencer:
    def __init__(self) -> None:
        self._n = 0

    def make(self, event_type: E, *, run_id=None, data=None) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, sequence=self._n, run_id=run_id, data=data or {})
        self._n += 1
        return event


class RuntimeStreamer:
    def __init__(self, orchestrator, *, checkpointer=None) -> None:
        self._orchestrator = orchestrator
        # Phase 41B: optional persistence for WAITING_* outcomes. An async
        # ``(AgentRunResult) -> checkpoint_id | None`` (the ResumeCoordinator's
        # checkpoint step) so a *streamed* waiting run is resumable via
        # /agent/resume — the terminal event then carries ``checkpoint_id``. When
        # None (default), the stream is byte-identical to Phase 38.
        self._checkpointer = checkpointer

    async def run_stream(
        self,
        user_request: str,
        user_id: str,
        thread_id: str | None = None,
        metadata: dict | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        seq = _Sequencer()
        yield seq.make(
            E.RUNTIME_STARTED,
            data={"user_request": user_request, "user_id": user_id, "thread_id": thread_id},
        )

        # The orchestrator emits pipeline events into this queue live via the
        # sink; the drain loop below yields them as they arrive, then the terminal
        # event is derived from how ``run`` finished.
        queue: asyncio.Queue = asyncio.Queue()

        async def sink(event_type: E, run_id, data: dict) -> None:
            await queue.put((event_type, run_id, data))

        outcome: dict = {}

        async def _drive() -> None:
            try:
                outcome["result"] = await self._orchestrator.run(
                    user_request, user_id, thread_id=thread_id, metadata=metadata,
                    stream_sink=sink,
                )
            except Exception as exc:  # noqa: BLE001 - surface as a terminal event
                outcome["error"] = exc
            finally:
                await queue.put(_SENTINEL)

        task = asyncio.create_task(_drive())
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                event_type, run_id, data = item
                yield seq.make(event_type, run_id=run_id, data=data)
        finally:
            # Normal completion: the drive task is already done. Early close
            # (client disconnect / cancellation): cancel the background run so no
            # orchestrator/provider work is orphaned (Phase 42A).
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            else:
                await task

        # Terminal event (never emitted mid-stream by the orchestrator).
        if "error" in outcome:
            exc = outcome["error"]
            yield seq.make(
                E.RUNTIME_FAILED,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        result = outcome["result"]
        # A provider failure the orchestrator converted into a FAILED outcome:
        # terminate with runtime_failed (API-safe metadata), never completed.
        if result.runtime_outcome == RuntimeOutcome.FAILED:
            yield seq.make(
                E.RUNTIME_FAILED,
                run_id=result.run_id,
                data={
                    "runtime_outcome": result.runtime_outcome.value,
                    "failure_stage": result.metadata.get("failure_stage"),
                    "error_code": result.metadata.get("error_code"),
                    "retryable": result.metadata.get("retryable"),
                    "reason": result.pending_reason,
                },
            )
            return

        # For a WAITING_* outcome, persist a checkpoint (if a checkpointer is
        # wired) so the run is resumable, and surface the id in the terminal event.
        # Best-effort: a persistence failure must not break the stream.
        checkpoint_id = None
        if self._checkpointer is not None and result.runtime_outcome in _WAITING_OUTCOMES:
            try:
                checkpoint_id = await self._checkpointer(result)
            except Exception:  # noqa: BLE001 - never let persistence break the stream
                checkpoint_id = None

        terminal_data = {
            "runtime_outcome": result.runtime_outcome.value,
            "thread_id": getattr(result, "thread_id", None),
            "pending_action": result.pending_action,
            "pending_reason": result.pending_reason,
            "checkpoint_id": checkpoint_id,
        }
        # Phase 43: a document-selection pause carries a SAFE candidate list
        # (document_id / filename / created_at only) so the UI can render a picker.
        candidates = result.metadata.get("document_candidates")
        if candidates is not None:
            terminal_data["document_candidates"] = candidates

        yield seq.make(E.RUNTIME_COMPLETED, run_id=result.run_id, data=terminal_data)
