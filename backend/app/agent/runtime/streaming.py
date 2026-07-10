"""Runtime streaming (Phase 32).

Exposes a runtime execution as an async stream of ``RuntimeEvent``s, without
changing any runtime decision, planning, or retrieval. ``RuntimeStreamer`` wraps
an injected orchestrator and adds ``run_stream()`` alongside the unchanged
``run()``.

Design note. ``run()`` is reused verbatim (no orchestration logic is
duplicated), and the runtime is deterministic and in-memory. So ``run_stream``
emits the lifecycle envelope live — ``runtime_started`` up front,
``runtime_failed`` on error — and reconstructs the per-stage events from the
completed run's RunContext, in execution order. A future token-streaming LLM
provider would emit ``answer_chunk`` events live; the deterministic provider
emits a single ``answer_completed`` (optionally chunked here for API parity).

Config-free and fully injectable: no LLM, no database, no settings. Never
inspects planner/evaluation/repair internals beyond the API-safe metadata the
runtime already recorded.
"""

from collections.abc import AsyncIterator

from app.agent.runtime.events import RuntimeEvent, RuntimeEventType as E


class _Sequencer:
    def __init__(self) -> None:
        self._n = 0

    def make(self, event_type: E, *, run_id=None, data=None) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, sequence=self._n, run_id=run_id, data=data or {})
        self._n += 1
        return event


class RuntimeStreamer:
    def __init__(self, orchestrator, *, chunk_answer: bool = False, chunk_size: int = 24) -> None:
        self._orchestrator = orchestrator
        self._chunk_answer = chunk_answer
        self._chunk_size = max(1, chunk_size)

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

        try:
            result = await self._orchestrator.run(
                user_request, user_id, thread_id=thread_id, metadata=metadata
            )
        except Exception as exc:  # noqa: BLE001 - surface as a terminal stream event
            yield seq.make(
                E.RUNTIME_FAILED,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        for event in self._stage_events(seq, result):
            yield event

    # -- Event reconstruction (reads only API-safe recorded metadata) --------

    def _stage_events(self, seq: _Sequencer, result) -> list[RuntimeEvent]:
        run_context = result.run_context
        metadata = run_context.metadata
        run_id = result.run_id
        events: list[RuntimeEvent] = []

        def add(event_type: E, **data) -> None:
            events.append(seq.make(event_type, run_id=run_id, data=data))

        # Context
        add(E.CONTEXT_STARTED)
        add(
            E.CONTEXT_COMPLETED,
            providers=metadata.get("context_providers"),
            context_size=len(run_context.working_context),
        )

        # Retrieval
        add(E.RETRIEVAL_STARTED)
        add(E.RETRIEVAL_COMPLETED, selected_capabilities=list(run_context.selected_capabilities))

        # Planner (planner path only)
        planner = metadata.get("planner_runtime")
        if result.behavior_path == "planner" and planner is not None:
            add(E.PLANNER_STARTED)
            add(
                E.PLANNER_COMPLETED,
                execution_order=planner.get("execution_order"),
                runtime_status=planner.get("runtime_status"),
            )

        # Tools (one pair per recorded tool output)
        for output in run_context.tool_outputs:
            add(E.TOOL_STARTED, capability_id=output.capability_id)
            add(
                E.TOOL_COMPLETED,
                capability_id=output.capability_id,
                output_keys=sorted(output.output.keys()),
            )

        # Evaluation
        evaluation = metadata.get("answer_evaluation")
        if evaluation is not None:
            add(E.EVALUATION_STARTED)
            add(
                E.EVALUATION_COMPLETED,
                passed=evaluation.get("passed"),
                overall_score=evaluation.get("overall_score"),
            )

        # Repair (one pair per repair round)
        for record in metadata.get("repair_rounds") or []:
            add(E.REPAIR_STARTED, action=record.get("action"))
            add(
                E.REPAIR_COMPLETED,
                action=record.get("action"),
                applied=record.get("applied"),
                target_stage=record.get("target_stage"),
            )

        # Answer
        answer = result.answer
        add(E.ANSWER_STARTED)
        if self._chunk_answer and answer.text:
            for chunk in self._chunks(answer.text):
                add(E.ANSWER_CHUNK, text=chunk)
        add(
            E.ANSWER_COMPLETED,
            text=answer.text,
            provider=answer.provider,
            model=answer.model,
        )

        # Terminal
        add(
            E.RUNTIME_COMPLETED,
            runtime_outcome=result.runtime_outcome.value,
            pending_action=result.pending_action,
            pending_reason=result.pending_reason,
        )
        return events

    def _chunks(self, text: str) -> list[str]:
        return [text[i : i + self._chunk_size] for i in range(0, len(text), self._chunk_size)]
