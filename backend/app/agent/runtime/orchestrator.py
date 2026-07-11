"""Runtime Orchestrator (Phase 18; Phase 22 evaluation + repair).

The single in-memory flow that chains every runtime stage end-to-end:

    ContextEngine.build → BehaviorGate.decide
      → DirectRuntime.run  (DIRECT)  |  PlannerRuntime.run (PLANNER)
      → FinalContextBuilder.build → FinalAnswerProvider.generate
      → attach_final_answer
      → [optional] AnswerEvaluationEngine → RepairRuntime → bounded regenerate
      → AgentRunResult

Every dependency is injected — the orchestrator owns sequencing only, not
construction. This keeps it deterministic and config-free: no LLM, no database,
no application settings, no production endpoint, no streaming. Planner reasoning
is not implemented here; a ``plan_source`` callable supplies the ExecutionPlan
for the PLANNER path (a static plan in tests). See ARCHITECTURE.md §5.

Phase 22 (additive). If an ``answer_evaluator`` is injected, the draft answer is
evaluated and, on failure, a ``RepairRuntime`` decides a repair. Only *local*
regeneration repairs (updated FinalPrompt → regenerate) are executed, bounded by
``max_repair_rounds``; deferred actions (retrieve_more_context, replan, HITL, …)
are recorded but not executed. With no evaluator, behavior is unchanged.

Phase 26 (additive). ``continue_run`` resumes a rehydrated RunContext (Phase 25)
without rebuilding context or minting a new run_id: WAITING_FOR_USER/APPROVAL
fold the resolution into a fresh FinalPrompt and regenerate (then evaluate/repair
as usual); WAITING_FOR_CONTEXT/REPLAN are surfaced as deferred, never faked.
"""

import inspect
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.evaluation.engine import attach_evaluation_report
from app.agent.interpret import is_document_inventory_request
from app.agent.llm.final_provider import (
    FinalAnswer,
    FinalAnswerProvider,
    attach_final_answer,
)
from app.agent.llm.planner_provider import (
    PlannerOutputValidationError,
    PlannerProviderError,
)
from app.agent.llm.provider_adapter import (
    FinalProviderError,
    ProviderError,
    ProviderUnavailableError,
)
from app.agent.models.final_prompt import FinalPrompt
from app.agent.repair.runtime import RepairRuntime
from app.agent.runtime import diagnostics
from app.agent.runtime.context import BehaviorPath, RunContext
from app.agent.runtime.events import RuntimeEventType as E
from app.agent.runtime.outcome import RuntimeOutcome, derive_runtime_outcome
from app.agent.runtime.planner_runtime import ExecutionPlan


class _StreamEmitter:
    """Emits ordered RuntimeEvents through an optional sink during ``run()``.

    Phase 38 streaming seam. When ``sink`` is None the emitter is a no-op, so the
    non-streaming ``/agent/run`` path is byte-identical to before — no events, no
    behavior change. ``streaming`` tells the answer seam whether to token-stream
    (via ``generate_stream``) or generate the whole answer at once. ``run_id`` is
    bound once the RunContext exists so every event can carry it.

    The sink is an async callable ``sink(event_type, run_id, data)``; sequence
    numbering and terminal (runtime_completed/failed) events belong to the caller
    (RuntimeStreamer), not the orchestrator.
    """

    def __init__(self, sink) -> None:
        self._sink = sink
        self.streaming = sink is not None
        self._run_id = None

    def bind_run_id(self, run_id) -> None:
        self._run_id = run_id

    async def __call__(self, event_type: E, **data) -> None:
        if self._sink is None:
            return
        await self._sink(event_type, self._run_id, data)


class OrchestratorError(Exception):
    """Base error for the Runtime Orchestrator."""


class MissingPlanSourceError(OrchestratorError):
    """Raised when the PLANNER path is taken but no plan_source was injected."""


# -- Injected-dependency contracts (duck-typed; kept import-light) ----------- #

class ContextEngineLike(Protocol):
    async def build(
        self, user_request: str, user_id: str, thread_id: str | None = None,
        metadata: dict | None = None,
    ) -> RunContext:
        ...


class BehaviorGateLike(Protocol):
    def decide(self, run_context: RunContext, attach: bool = True): ...


class DirectRuntimeLike(Protocol):
    async def run(self, run_context: RunContext) -> RunContext: ...


class PlannerRuntimeLike(Protocol):
    async def run(self, run_context: RunContext, plan: ExecutionPlan) -> RunContext: ...


class FinalContextBuilderLike(Protocol):
    def build(self, run_context: RunContext) -> FinalPrompt: ...


class PlanSource(Protocol):
    def __call__(self, run_context: RunContext) -> ExecutionPlan: ...


class AnswerEvaluatorLike(Protocol):
    def evaluate(self, final_prompt: FinalPrompt, final_answer: FinalAnswer, run_context=None): ...


class RepairRuntimeLike(Protocol):
    def repair(self, run_context, final_prompt, final_answer, evaluation_report): ...


class AgentRunResult(BaseModel):
    """Structured result of a single orchestrated agent run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    user_id: str
    thread_id: str | None = None
    behavior_path: str
    answer: FinalAnswer
    final_prompt: FinalPrompt
    run_context: RunContext
    runtime_outcome: RuntimeOutcome = RuntimeOutcome.COMPLETED
    pending_action: str | None = None
    pending_reason: str | None = None
    metadata: dict = Field(default_factory=dict)


class AgentOrchestrator:
    def __init__(
        self,
        *,
        context_engine: ContextEngineLike,
        behavior_gate: BehaviorGateLike,
        direct_runtime: DirectRuntimeLike,
        planner_runtime: PlannerRuntimeLike,
        final_context_builder: FinalContextBuilderLike,
        final_provider: FinalAnswerProvider,
        plan_source: PlanSource | None = None,
        planner_provider=None,
        capability_retriever=None,
        planner_top_k: int = 8,
        answer_evaluator: AnswerEvaluatorLike | None = None,
        repair_runtime: RepairRuntimeLike | None = None,
        max_repair_rounds: int = 1,
        scope_gate=None,
        document_inventory_fn=None,
    ) -> None:
        self._context_engine = context_engine
        # Phase 43: an optional scope gate resolves document references (and can
        # pause for a genuine document-selection clarification) before execution.
        # Default None → byte-identical to before.
        self._scope_gate = scope_gate
        # Phase 46.1: an optional async ``(user_id, thread_id) -> list[dict]`` that
        # returns the ACTIVE thread's own document records (ownership-scoped, all
        # statuses). Used only by the deterministic document-inventory fast path,
        # which bypasses retrieval/planner/LLM entirely. Default None → no fast
        # path (byte-identical to before).
        self._document_inventory_fn = document_inventory_fn
        self._behavior_gate = behavior_gate
        self._direct_runtime = direct_runtime
        self._planner_runtime = planner_runtime
        self._final_context_builder = final_context_builder
        self._final_provider = final_provider
        self._plan_source = plan_source
        # Phase 36: on the PLANNER path, request a typed ExecutionPlan from an
        # injected PlannerProvider (built from top-k capabilities), falling back
        # to the static plan_source. DIRECT never touches either.
        self._planner_provider = planner_provider
        self._capability_retriever = capability_retriever
        self._planner_top_k = planner_top_k
        self._answer_evaluator = answer_evaluator
        # A repair runtime is only needed when evaluation is enabled.
        self._repair_runtime = repair_runtime or (
            RepairRuntime() if answer_evaluator is not None else None
        )
        self._max_repair_rounds = max(0, max_repair_rounds)

    async def run(
        self,
        user_request: str,
        user_id: str,
        thread_id: str | None = None,
        metadata: dict | None = None,
        *,
        stream_sink=None,
    ) -> AgentRunResult:
        # Phase 38: an optional ``stream_sink`` turns this into a live event
        # source. When absent (``/agent/run``) ``emit`` is a no-op and the whole
        # pipeline is byte-identical to the non-streaming behavior.
        emit = _StreamEmitter(stream_sink)

        # 1. Build the RunContext (working context assembled by the engine).
        await emit(E.CONTEXT_STARTED)
        run_context = await self._context_engine.build(
            user_request, user_id, thread_id=thread_id, metadata=metadata
        )
        emit.bind_run_id(run_context.run_id)
        await emit(
            E.CONTEXT_COMPLETED,
            providers=run_context.metadata.get("context_providers"),
            context_size=len(run_context.working_context),
        )

        # 1a. Document inventory fast path (Phase 46.1). A deterministic listing
        # request ("what documents are uploaded?") is answered by listing the
        # active thread's OWN document records — bypassing the scope gate,
        # behavior gate, capability retrieval, planner, document chunk retrieval,
        # embeddings, reranker, and the final LLM. This both fixes the routing
        # defect (such a request must never trigger document-content retrieval)
        # and guarantees no stale/foreign evidence or E# ids can appear.
        if self._document_inventory_fn is not None and is_document_inventory_request(
            user_request
        ):
            return await self._document_inventory_result(run_context, emit)

        # 1b. Scope Gate (Phase 43) — resolve document references before execution.
        # An ambiguous/unauthorized reference pauses the run for a genuine
        # document-selection clarification (WAITING_FOR_USER). A resolved
        # reference attaches the document evidence to the RunContext.
        if self._scope_gate is not None:
            decision = await self._scope_gate.evaluate(run_context)
            if decision.action == "clarify":
                return self._scope_clarification_result(run_context, decision)

        # 2. Behavior Gate — attaches behavior_profile + metadata["behavior_decision"].
        self._behavior_gate.decide(run_context)
        path = run_context.behavior_profile.path
        # Diagnostics (Phase 46.2.3): which execution path this request takes.
        diagnostics.runtime_path_selected(
            run_context, path=path.value, reason=run_context.behavior_profile.reason
        )

        # 3. Dispatch to the one execution engine (planner orchestrates direct).
        # Planner-provider failures never execute a guessed plan — they convert
        # to a safe RuntimeOutcome. Only DOMAIN provider errors are caught;
        # programming bugs still propagate.
        await emit(E.RETRIEVAL_STARTED)
        if path == BehaviorPath.PLANNER:
            try:
                plan = await self._resolve_plan(run_context)
            except (PlannerProviderError, ProviderUnavailableError) as exc:
                return self._provider_failure_result(
                    run_context, path.value, stage="planner_provider", exc=exc
                )
            # Diagnostics (Phase 46.2.3): the plan the planner produced (task ids +
            # safe request fingerprints only — never the raw request or reasoning).
            diagnostics.emit(
                run_context, "agent.plan_created",
                task_count=len(plan.tasks),
                tasks=[
                    {"task_id": t.id, "request_hash": diagnostics.hash12(t.request),
                     "request_length": len(t.request or ""), "optional": t.optional}
                    for t in plan.tasks
                ],
            )
            run_context = await self._planner_runtime.run(run_context, plan)
        else:
            run_context = await self._direct_runtime.run(run_context)
        await emit(
            E.RETRIEVAL_COMPLETED,
            selected_capabilities=list(run_context.selected_capabilities),
        )

        if path == BehaviorPath.PLANNER:
            planner = run_context.metadata.get("planner_runtime") or {}
            await emit(E.PLANNER_STARTED)
            await emit(
                E.PLANNER_COMPLETED,
                execution_order=planner.get("execution_order"),
                runtime_status=planner.get("runtime_status"),
            )

        for output in run_context.tool_outputs:
            await emit(E.TOOL_STARTED, capability_id=output.capability_id)
            await emit(
                E.TOOL_COMPLETED,
                capability_id=output.capability_id,
                output_keys=sorted(output.output.keys()),
            )

        # 4-6. Build the final prompt, generate (live-streaming the answer when a
        # sink is present), and record the draft answer.
        final_prompt = self._final_context_builder.build(run_context)
        try:
            answer = await self._generate_answer(final_prompt, emit)
        except (FinalProviderError, ProviderUnavailableError) as exc:
            return self._provider_failure_result(
                run_context, path.value, stage="final_provider", exc=exc,
                final_prompt=final_prompt,
            )
        attach_final_answer(run_context, answer)

        result_metadata = {
            "behavior_decision": run_context.metadata.get("behavior_decision"),
            "execution_status": run_context.metadata.get("execution_status"),
            "runtime_status": run_context.metadata.get("planner_runtime", {}).get(
                "runtime_status"
            ),
            "provider": answer.provider,
            "model": answer.model,
        }

        # 4b (optional). Evaluate the draft and apply bounded local repair.
        evaluator_ran = self._answer_evaluator is not None
        report = None
        terminal_repair = None
        if evaluator_ran:
            final_prompt, answer, report, records, terminal_repair = (
                await self._evaluate_and_repair(run_context, final_prompt, answer, emit)
            )
            result_metadata.update(
                {
                    "evaluation_passed": report.passed,
                    "evaluation_score": report.overall_score,
                    "repair_rounds": len(records),
                    "repair_actions": [r["action"] for r in records],
                    "provider": answer.provider,
                    "model": answer.model,
                }
            )

        # Derive the terminal runtime outcome (contract for API/UI/workers/HITL).
        # Deferred repairs are exposed here, never executed.
        outcome, pending_action, pending_reason = derive_runtime_outcome(
            evaluator_ran, report, terminal_repair
        )
        result_metadata["runtime_outcome"] = outcome.value
        run_context.metadata["runtime_outcome"] = outcome.value

        # 7. Structured result.
        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path=path.value,
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=outcome,
            pending_action=pending_action,
            pending_reason=pending_reason,
            metadata=result_metadata,
        )

    async def _generate_answer(self, final_prompt: FinalPrompt, emit) -> FinalAnswer:
        """The single answer-generation seam (Phase 38).

        Non-streaming (no sink): call ``generate`` — byte-identical to before.
        Streaming: emit ``answer_started``, one ``answer_chunk`` per provider
        chunk *live*, assemble the complete draft, build the FinalAnswer, then
        emit ``answer_completed``. Providers predating the streaming contract fall
        back to ``generate`` (emitted as one chunk). Evaluation never sees partial
        chunks — only the assembled answer this method returns.
        """
        if not emit.streaming:
            return await self._final_provider.generate(final_prompt)

        await emit(E.ANSWER_STARTED)
        stream = getattr(self._final_provider, "generate_stream", None)
        build = getattr(self._final_provider, "build_final_answer", None)
        if stream is None or build is None:
            # Provider predates the streaming contract: still a live event, just
            # not token-by-token.
            answer = await self._final_provider.generate(final_prompt)
            if answer.text:
                await emit(E.ANSWER_CHUNK, text=answer.text)
        else:
            chunks: list[str] = []
            async for chunk in stream(final_prompt):
                chunks.append(chunk)
                await emit(E.ANSWER_CHUNK, text=chunk)
            answer = build(final_prompt, "".join(chunks))

        await emit(
            E.ANSWER_COMPLETED,
            text=answer.text,
            provider=answer.provider,
            model=answer.model,
        )
        return answer

    async def _evaluate_and_repair(self, run_context, final_prompt, answer, emit=None):
        """Evaluate the draft; on failure, run bounded local regeneration.

        Only repairs that return an ``updated_final_prompt`` (regenerate_*) are
        executed here, capped by ``max_repair_rounds``. Terminal local repairs
        (partial/fail) and deferred hand-offs are recorded, not executed. When
        streaming, a regeneration repair produces a *second bounded stream round*
        (new answer_started/chunk/completed) via ``_generate_answer``.
        """
        emit = emit or _StreamEmitter(None)
        evaluator = self._answer_evaluator
        repair_runtime = self._repair_runtime

        await emit(E.EVALUATION_STARTED)
        report = evaluator.evaluate(final_prompt, answer, run_context)
        attach_evaluation_report(run_context, report)
        await emit(
            E.EVALUATION_COMPLETED, passed=report.passed, overall_score=report.overall_score
        )

        records: list[dict] = []
        terminal_repair = None
        rounds = 0
        while not report.passed and rounds < self._max_repair_rounds:
            result = repair_runtime.repair(run_context, final_prompt, answer, report)
            terminal_repair = result
            await emit(E.REPAIR_STARTED, action=result.action.value)
            records.append(
                {
                    "round": rounds + 1,
                    "action": result.action.value,
                    "applied": result.applied,
                    "target_stage": result.target_stage,
                    "reason": result.reason,
                }
            )
            await emit(
                E.REPAIR_COMPLETED,
                action=result.action.value,
                applied=result.applied,
                target_stage=result.target_stage,
            )

            # Only a local regeneration repair produces an updated prompt.
            if result.applied and result.updated_final_prompt is not None:
                final_prompt = result.updated_final_prompt
                answer = await self._generate_answer(final_prompt, emit)
                attach_final_answer(run_context, answer)
                rounds += 1
                await emit(E.EVALUATION_STARTED)
                report = evaluator.evaluate(final_prompt, answer, run_context)
                attach_evaluation_report(run_context, report)
                await emit(
                    E.EVALUATION_COMPLETED,
                    passed=report.passed,
                    overall_score=report.overall_score,
                )
                continue

            # Terminal local repair (partial/fail) or deferred hand-off: stop.
            break

        run_context.metadata["repair_rounds"] = records
        return final_prompt, answer, report, records, terminal_repair

    async def _resolve_plan(self, run_context: RunContext) -> ExecutionPlan:
        # Prefer the typed PlannerProvider (Phase 36); fall back to plan_source.
        if self._planner_provider is not None:
            prompt = self._build_planner_prompt(run_context)
            return await self._planner_provider.plan(prompt)
        if self._plan_source is None:
            raise MissingPlanSourceError(
                "PLANNER path requires an injected planner_provider or plan_source"
            )
        plan = self._plan_source(run_context)
        if inspect.isawaitable(plan):
            plan = await plan
        return plan

    def _build_planner_prompt(self, run_context: RunContext):
        from app.agent.models.planner_prompt import build_planner_prompt

        matches = []
        if self._capability_retriever is not None:
            matches = self._capability_retriever.retrieve_for_run_context(
                run_context, top_k=self._planner_top_k
            ).matches
        # Diagnostics (Phase 46.2.3): the exact candidate set (ranked) handed to the
        # planner. This retrieval uses the context-enriched query (no request-only
        # override), so it reveals whether working-context pollution reorders the
        # candidates the planner chooses from.
        diagnostics.capability_candidates(run_context, matches, path="planner")
        diagnostics.emit(
            run_context, "agent.planner_candidates",
            candidate_ids=[m.tool.id for m in matches],
        )
        return build_planner_prompt(run_context, matches)

    def _provider_failure_result(
        self,
        run_context: RunContext,
        behavior_path: str,
        *,
        stage: str,
        exc: ProviderError,
        final_prompt: FinalPrompt | None = None,
    ) -> AgentRunResult:
        """Convert a domain provider failure into an API-safe AgentRunResult.

        No vendor detail is exposed — only the error's ``safe_message`` /
        ``error_code`` / ``retryable``. Evaluation/repair is NOT run (there is no
        valid draft answer). A validation-level planner failure degrades to
        WAITING_FOR_USER (a clarification may help); everything else is FAILED.
        """
        error_code = getattr(exc, "error_code", "provider_error")
        retryable = bool(getattr(exc, "retryable", False))
        safe_message = getattr(exc, "safe_message", "The request could not be completed.")
        clarification_needed = bool(getattr(exc, "clarification_needed", False))

        if isinstance(exc, PlannerOutputValidationError):
            outcome = RuntimeOutcome.WAITING_FOR_USER
            pending_action = "ask_user_for_clarification"
        else:
            outcome = RuntimeOutcome.FAILED
            pending_action = None

        if final_prompt is None:
            final_prompt = self._final_context_builder.build(run_context)

        # Placeholder answer carrying only the safe message (no vendor text).
        answer = FinalAnswer(
            text=safe_message, provider="", model="", finish_reason="error",
            metadata={"error": True, "error_code": error_code},
        )
        attach_final_answer(run_context, answer)

        run_context.metadata["runtime_outcome"] = outcome.value
        run_context.metadata["provider_failure"] = {
            "stage": stage, "error_code": error_code, "retryable": retryable,
            "error_type": type(exc).__name__,
        }

        result_metadata = {
            "behavior_decision": run_context.metadata.get("behavior_decision"),
            "execution_status": run_context.metadata.get("execution_status"),
            "provider": answer.provider,
            "model": answer.model,
            "failure_stage": stage,
            "error_code": error_code,
            "retryable": retryable,
            "clarification_needed": clarification_needed,
            "runtime_outcome": outcome.value,
        }
        if stage == "planner_provider":
            result_metadata["planner_error_type"] = type(exc).__name__

        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path=behavior_path,
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=outcome,
            pending_action=pending_action,
            pending_reason=safe_message,
            metadata=result_metadata,
        )

    def _scope_clarification_result(self, run_context: RunContext, decision) -> AgentRunResult:
        """A genuine WAITING_FOR_USER pause for document-selection ambiguity.

        Carries a SAFE candidate list (document_id / filename / created_at only)
        in metadata so the UI can render a picker. No answer is generated and no
        retrieval is performed until the user selects."""
        from app.agent.runtime.scope_gate import SELECT_DOCUMENT_ACTION

        reason = decision.pending_reason or "Please select which document you mean."
        final_prompt = self._final_context_builder.build(run_context)
        answer = FinalAnswer(
            text=reason, provider="", model="", finish_reason="waiting",
            metadata={"waiting": True, "pending_action": SELECT_DOCUMENT_ACTION},
        )
        attach_final_answer(run_context, answer)

        run_context.metadata["runtime_outcome"] = RuntimeOutcome.WAITING_FOR_USER.value
        run_context.metadata["document_candidates"] = decision.candidates

        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path="direct",
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=RuntimeOutcome.WAITING_FOR_USER,
            pending_action=SELECT_DOCUMENT_ACTION,
            pending_reason=reason,
            metadata={
                "runtime_outcome": RuntimeOutcome.WAITING_FOR_USER.value,
                "pending_action": SELECT_DOCUMENT_ACTION,
                "document_candidates": decision.candidates,
                "document_scope": decision.metadata.get("document_scope"),
                "clarification_needed": True,
            },
        )

    async def _document_inventory_result(self, run_context: RunContext, emit) -> AgentRunResult:
        """Deterministic document-inventory answer (Phase 46.1).

        Lists the ACTIVE thread's own document records (ownership-scoped by
        user_id + thread_id) via the injected inventory function, formats them
        deterministically, and returns a COMPLETED result — with NO evidence, NO
        tool outputs, NO retrieval, NO planner, and NO LLM. The evidence list stays
        empty, so no stale/foreign content and no E# citations can appear."""
        from app.agent.documents import format_document_inventory

        # Diagnostics (Phase 46.2.3): the deterministic fast path was taken.
        diagnostics.runtime_path_selected(run_context, path="deterministic_fast_path")

        documents: list[dict] = []
        if run_context.thread_id:
            documents = list(
                await self._document_inventory_fn(run_context.user_id, run_context.thread_id) or []
            )
        text = format_document_inventory(documents)

        # Safe routing/runtime metadata (no filenames, no content).
        run_context.metadata["resolved_intent"] = "document_inventory"
        run_context.metadata["deterministic_fast_path"] = True
        run_context.metadata["document_count"] = len(documents)
        run_context.metadata["runtime_outcome"] = RuntimeOutcome.COMPLETED.value
        from app.logging_config import get_logger

        get_logger("orchestrator").info(
            "orchestrator.document_inventory",
            extra={
                "resolved_intent": "document_inventory",
                "deterministic_fast_path": True,
                "document_count": len(documents),
            },
        )

        # Live-stream the deterministic text so streaming and non-streaming
        # produce the same answer (emit is a no-op without a sink).
        if emit.streaming:
            await emit(E.ANSWER_STARTED)
            if text:
                await emit(E.ANSWER_CHUNK, text=text)
            await emit(
                E.ANSWER_COMPLETED,
                text=text,
                provider="deterministic-inventory",
                model="document-inventory-1",
            )

        answer = FinalAnswer(
            text=text,
            used_citations=[],
            provider="deterministic-inventory",
            model="document-inventory-1",
            finish_reason="stop",
            metadata={
                "grounded": True,
                "deterministic_fast_path": True,
                "resolved_intent": "document_inventory",
                "document_count": len(documents),
                "evidence_used": 0,
                "tool_outputs_used": 0,
            },
        )
        attach_final_answer(run_context, answer)

        final_prompt = self._final_context_builder.build(run_context)
        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path="direct",
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=RuntimeOutcome.COMPLETED,
            metadata={
                "runtime_outcome": RuntimeOutcome.COMPLETED.value,
                "resolved_intent": "document_inventory",
                "deterministic_fast_path": True,
                "document_count": len(documents),
                "provider": answer.provider,
                "model": answer.model,
            },
        )

    # -- Resume continuation (Phase 26) --------------------------------------

    async def continue_run(self, run_context: RunContext) -> AgentRunResult:
        """Continue a *rehydrated* RunContext after a resume (Phase 25).

        The RunContext already carries its working context, behavior profile,
        prior outputs, and ``metadata['resume']``. Continuation never rebuilds
        context from the ContextEngine, re-authenticates, or mints a new run_id.
        WAITING_FOR_USER / WAITING_FOR_APPROVAL fold the resolution into a fresh
        FinalPrompt and regenerate; WAITING_FOR_CONTEXT / WAITING_FOR_REPLAN are
        surfaced as deferred (not executed) rather than faking retrieval/replan.
        """
        resume = run_context.metadata.get("resume") or {}
        prior_outcome = self._coerce_outcome(
            resume.get("runtime_outcome") or run_context.metadata.get("runtime_outcome")
        )
        behavior_path = (
            run_context.behavior_profile.path.value
            if run_context.behavior_profile is not None
            else "direct"
        )

        if prior_outcome in (RuntimeOutcome.WAITING_FOR_USER, RuntimeOutcome.WAITING_FOR_APPROVAL):
            # Phase 43: a document-selection resume re-runs the scope gate with the
            # user's picked ids (validated against the owned set) to attach the
            # resolved evidence — or re-clarify if still ambiguous — before
            # generating the grounded answer over the SAME run.
            if self._scope_gate is not None and resume.get("pending_action") == "select_document":
                decision = await self._scope_gate.evaluate(run_context, is_resume=True)
                if decision.action == "clarify":
                    return self._scope_clarification_result(run_context, decision)
            return await self._continue_generation(run_context, resume, behavior_path)
        return self._defer_continuation(run_context, resume, prior_outcome, behavior_path)

    async def _continue_generation(self, run_context, resume, behavior_path) -> AgentRunResult:
        # Rebuild the final prompt from current state and fold in the resolution.
        final_prompt = self._fold_resume(self._final_context_builder.build(run_context), resume)
        answer = await self._final_provider.generate(final_prompt)
        attach_final_answer(run_context, answer)

        result_metadata = {
            "behavior_decision": run_context.metadata.get("behavior_decision"),
            "execution_status": run_context.metadata.get("execution_status"),
            "runtime_status": run_context.metadata.get("planner_runtime", {}).get("runtime_status"),
            "provider": answer.provider,
            "model": answer.model,
            "resumed": True,
            "resume_kind": resume.get("kind"),
        }

        evaluator_ran = self._answer_evaluator is not None
        report = None
        terminal_repair = None
        if evaluator_ran:
            final_prompt, answer, report, records, terminal_repair = (
                await self._evaluate_and_repair(run_context, final_prompt, answer)
            )
            result_metadata.update(
                {
                    "evaluation_passed": report.passed,
                    "evaluation_score": report.overall_score,
                    "repair_rounds": len(records),
                    "repair_actions": [r["action"] for r in records],
                    "provider": answer.provider,
                    "model": answer.model,
                }
            )

        outcome, pending_action, pending_reason = derive_runtime_outcome(
            evaluator_ran, report, terminal_repair
        )
        result_metadata["runtime_outcome"] = outcome.value
        run_context.metadata["runtime_outcome"] = outcome.value

        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path=behavior_path,
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=outcome,
            pending_action=pending_action,
            pending_reason=pending_reason,
            metadata=result_metadata,
        )

    def _defer_continuation(self, run_context, resume, prior_outcome, behavior_path) -> AgentRunResult:
        # Deferred: do NOT execute re-retrieval or replan. Curate a final prompt
        # (deterministic, no retrieval) and re-surface the waiting state.
        final_prompt = self._final_context_builder.build(run_context)
        prior = run_context.metadata.get("final_answer") or {}
        answer = FinalAnswer(
            text=prior.get("text", ""),
            used_citations=list(prior.get("used_citations", [])),
            usage_metadata=dict(prior.get("usage_metadata", {})),
            provider=prior.get("provider", ""),
            model=prior.get("model", ""),
            finish_reason=prior.get("finish_reason", "deferred"),
            metadata=dict(prior.get("metadata", {})),
        )
        outcome = prior_outcome or RuntimeOutcome.WAITING_FOR_CONTEXT
        pending_action = resume.get("pending_action")
        pending_reason = f"continuation for {outcome.value} is not executable in this phase"

        run_context.metadata["runtime_outcome"] = outcome.value
        run_context.metadata["continuation"] = {
            "deferred": True,
            "outcome": outcome.value,
            "pending_action": pending_action,
            "resume_kind": resume.get("kind"),
        }
        return AgentRunResult(
            run_id=run_context.run_id,
            user_id=run_context.user_id,
            thread_id=run_context.thread_id,
            behavior_path=behavior_path,
            answer=answer,
            final_prompt=final_prompt,
            run_context=run_context,
            runtime_outcome=outcome,
            pending_action=pending_action,
            pending_reason=pending_reason,
            metadata={
                "behavior_decision": run_context.metadata.get("behavior_decision"),
                "execution_status": run_context.metadata.get("execution_status"),
                "provider": answer.provider,
                "model": answer.model,
                "resumed": True,
                "deferred": True,
                "resume_kind": resume.get("kind"),
                "runtime_outcome": outcome.value,
            },
        )

    @staticmethod
    def _fold_resume(final_prompt: FinalPrompt, resume: dict) -> FinalPrompt:
        kind = resume.get("kind")
        value = resume.get("value")
        pending = resume.get("pending_action")
        verb = {
            "approval": "approved this step; proceed and produce the final answer.",
            "rejection": "rejected this step; do not proceed — explain what was not done.",
            "clarification": f"provided this clarification: {value!r}. Incorporate it into the answer.",
            "context_available": "indicated new context is available.",
            "replan_requested": "requested a re-plan.",
        }.get(kind, f"provided a {kind} resolution.")
        note = f"RESUME: the run was waiting on '{pending}'. The user {verb}"
        return final_prompt.model_copy(
            update={
                "final_instructions": f"{final_prompt.final_instructions}\n\n{note}",
                "metadata": {**final_prompt.metadata, "resume": dict(resume)},
            }
        )

    @staticmethod
    def _coerce_outcome(value):
        if isinstance(value, RuntimeOutcome):
            return value
        try:
            return RuntimeOutcome(value)
        except (ValueError, TypeError):
            return None
