"""Repair Runtime (Phase 21).

Applies a ``RepairDecision`` (from the Phase 20 evaluation) to produce a
``RepairResult``. This is a deterministic *shell*: it modifies prompts/context
metadata for the repairs it can do locally, and for the rest it names the stage
that must run next without executing it. It is NOT wired into the orchestrator
yet, and there is no repair *loop* here — one call applies at most one action.

Handled locally (applied=True):
- regenerate_with_same_context        — same prompt, tagged with repair metadata
- regenerate_with_stronger_instructions — appends a corrective directive
- return_partial_with_warning          — records a partial-answer warning
- fail_gracefully                      — records a graceful-failure message

Deferred (applied=False, target_stage named, not executed):
- retrieve_more_context (context_engine), rerun_capability (direct_runtime),
  replan (planner), ask_user_for_clarification / human_review (orchestrator).

Constraints: no LLM, no tool execution, no database; never mutates
``working_context`` (only append-only ``metadata`` writes); bounded — regenerate
actions honor ``RepairDecision.max_attempts`` and never loop.
"""

from app.agent.evaluation.models import EvaluationReport, RepairAction
from app.agent.models.final_prompt import FinalPrompt
from app.agent.repair.models import RepairResult
from app.agent.runtime.context import RunContext

_DEFERRED_STAGES = {
    RepairAction.RETRIEVE_MORE_CONTEXT: "context_engine",
    RepairAction.RERUN_CAPABILITY: "direct_runtime",
    RepairAction.REPLAN: "planner",
    RepairAction.ASK_USER_FOR_CLARIFICATION: "orchestrator",
    RepairAction.HUMAN_REVIEW: "orchestrator",
}


class RepairRuntime:
    STRONGER_DIRECTIVE = (
        "Ground every claim strictly in the cited evidence and tool outputs, "
        "address every part of the user's request, and explicitly disclose any "
        "partial or failed steps."
    )

    def __init__(self, *, max_attempts_cap: int = 3) -> None:
        self._cap = max(1, max_attempts_cap)

    def repair(
        self,
        run_context: RunContext,
        final_prompt: FinalPrompt,
        final_answer,
        evaluation_report: EvaluationReport,
    ) -> RepairResult:
        decision = evaluation_report.repair_decision
        action = decision.action

        if action == RepairAction.NONE:
            return RepairResult(
                action=action, applied=False,
                reason="no repair needed", target_stage=None,
            )

        if action == RepairAction.REGENERATE_WITH_SAME_CONTEXT:
            return self._regenerate(run_context, final_prompt, decision, stronger=False)
        if action == RepairAction.REGENERATE_WITH_STRONGER_INSTRUCTIONS:
            return self._regenerate(run_context, final_prompt, decision, stronger=True)
        if action == RepairAction.RETURN_PARTIAL_WITH_WARNING:
            return self._return_partial(run_context, decision)
        if action == RepairAction.FAIL_GRACEFULLY:
            return self._fail_gracefully(run_context, decision)

        return self._defer(action, decision)

    # -- Local repairs -------------------------------------------------------

    def _regenerate(self, run_context, final_prompt, decision, *, stronger: bool) -> RepairResult:
        attempts = self._attempts(run_context)
        limit = min(self._cap, max(1, decision.max_attempts or 1))
        if attempts >= limit:
            return RepairResult(
                action=decision.action, applied=False,
                reason="repair attempts exhausted", target_stage="orchestrator",
                metadata={"attempts": attempts, "max_attempts": limit, "exhausted": True},
            )

        attempt = attempts + 1
        prompt_update: dict = {
            "metadata": {
                **final_prompt.metadata,
                "repair": {
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "attempt": attempt,
                },
            }
        }
        if stronger:
            prompt_update["final_instructions"] = (
                f"{final_prompt.final_instructions}\n\n"
                f"REPAIR: a previous draft was rejected ({decision.reason}). "
                f"{self.STRONGER_DIRECTIVE}"
            )
        updated_prompt = final_prompt.model_copy(update=prompt_update)

        self._record_attempt(run_context, decision, attempt)
        return RepairResult(
            action=decision.action,
            applied=True,
            reason=("regenerate with stronger instructions" if stronger
                    else "regenerate with same context"),
            target_stage="final_provider",
            updated_final_prompt=updated_prompt,
            updated_run_context=run_context,
            metadata={"attempt": attempt, "max_attempts": limit, "stronger": stronger},
        )

    def _return_partial(self, run_context, decision) -> RepairResult:
        warning = (
            "Returning a partial answer: "
            + (decision.reason or "some steps could not be completed.")
        )
        run_context.metadata["repair_warning"] = warning
        return RepairResult(
            action=decision.action, applied=True,
            reason="returning partial answer with warning", target_stage="orchestrator",
            updated_run_context=run_context,
            metadata={"warning": warning, "status": "partial"},
        )

    def _fail_gracefully(self, run_context, decision) -> RepairResult:
        message = (
            "The request could not be completed: "
            + (decision.reason or "answer evaluation failed.")
        )
        run_context.metadata["repair_failure"] = message
        return RepairResult(
            action=decision.action, applied=True,
            reason="failing gracefully", target_stage="orchestrator",
            updated_run_context=run_context,
            metadata={"failure": message, "status": "failed"},
        )

    # -- Deferred hand-offs --------------------------------------------------

    def _defer(self, action: RepairAction, decision) -> RepairResult:
        stage = _DEFERRED_STAGES[action]
        metadata: dict = {"status": "deferred", "note": "stage not executed in Phase 21"}
        if action == RepairAction.ASK_USER_FOR_CLARIFICATION:
            metadata.update({"status": "waiting", "waiting_for": "user_input"})
        elif action == RepairAction.HUMAN_REVIEW:
            metadata.update({"status": "waiting", "waiting_for": "human_approval"})
        return RepairResult(
            action=action, applied=False,
            reason=f"repair requires stage '{stage}' (deferred)",
            target_stage=stage, metadata=metadata,
        )

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _attempts(run_context: RunContext) -> int:
        value = run_context.metadata.get("repair_attempts", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _record_attempt(run_context: RunContext, decision, attempt: int) -> None:
        run_context.metadata["repair_attempts"] = attempt
        history = run_context.metadata.setdefault("repair_history", [])
        history.append(
            {"action": decision.action.value, "reason": decision.reason, "attempt": attempt}
        )
