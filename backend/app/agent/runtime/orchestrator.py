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
from app.agent.llm.final_provider import (
    FinalAnswer,
    FinalAnswerProvider,
    attach_final_answer,
)
from app.agent.models.final_prompt import FinalPrompt
from app.agent.repair.runtime import RepairRuntime
from app.agent.runtime.context import BehaviorPath, RunContext
from app.agent.runtime.outcome import RuntimeOutcome, derive_runtime_outcome
from app.agent.runtime.planner_runtime import ExecutionPlan


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
        answer_evaluator: AnswerEvaluatorLike | None = None,
        repair_runtime: RepairRuntimeLike | None = None,
        max_repair_rounds: int = 1,
    ) -> None:
        self._context_engine = context_engine
        self._behavior_gate = behavior_gate
        self._direct_runtime = direct_runtime
        self._planner_runtime = planner_runtime
        self._final_context_builder = final_context_builder
        self._final_provider = final_provider
        self._plan_source = plan_source
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
    ) -> AgentRunResult:
        # 1. Build the RunContext (working context assembled by the engine).
        run_context = await self._context_engine.build(
            user_request, user_id, thread_id=thread_id, metadata=metadata
        )

        # 2. Behavior Gate — attaches behavior_profile + metadata["behavior_decision"].
        self._behavior_gate.decide(run_context)
        path = run_context.behavior_profile.path

        # 3. Dispatch to the one execution engine (planner orchestrates direct).
        if path == BehaviorPath.PLANNER:
            plan = await self._resolve_plan(run_context)
            run_context = await self._planner_runtime.run(run_context, plan)
        else:
            run_context = await self._direct_runtime.run(run_context)

        # 4-6. Build the final prompt, generate, and record the draft answer.
        final_prompt = self._final_context_builder.build(run_context)
        answer = await self._final_provider.generate(final_prompt)
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

    async def _evaluate_and_repair(self, run_context, final_prompt, answer):
        """Evaluate the draft; on failure, run bounded local regeneration.

        Only repairs that return an ``updated_final_prompt`` (regenerate_*) are
        executed here, capped by ``max_repair_rounds``. Terminal local repairs
        (partial/fail) and deferred hand-offs are recorded, not executed.
        """
        evaluator = self._answer_evaluator
        repair_runtime = self._repair_runtime

        report = evaluator.evaluate(final_prompt, answer, run_context)
        attach_evaluation_report(run_context, report)

        records: list[dict] = []
        terminal_repair = None
        rounds = 0
        while not report.passed and rounds < self._max_repair_rounds:
            result = repair_runtime.repair(run_context, final_prompt, answer, report)
            terminal_repair = result
            records.append(
                {
                    "round": rounds + 1,
                    "action": result.action.value,
                    "applied": result.applied,
                    "target_stage": result.target_stage,
                    "reason": result.reason,
                }
            )

            # Only a local regeneration repair produces an updated prompt.
            if result.applied and result.updated_final_prompt is not None:
                final_prompt = result.updated_final_prompt
                answer = await self._final_provider.generate(final_prompt)
                attach_final_answer(run_context, answer)
                rounds += 1
                report = evaluator.evaluate(final_prompt, answer, run_context)
                attach_evaluation_report(run_context, report)
                continue

            # Terminal local repair (partial/fail) or deferred hand-off: stop.
            break

        run_context.metadata["repair_rounds"] = records
        return final_prompt, answer, report, records, terminal_repair

    async def _resolve_plan(self, run_context: RunContext) -> ExecutionPlan:
        if self._plan_source is None:
            raise MissingPlanSourceError(
                "PLANNER path requires an injected plan_source"
            )
        plan = self._plan_source(run_context)
        if inspect.isawaitable(plan):
            plan = await plan
        return plan

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
