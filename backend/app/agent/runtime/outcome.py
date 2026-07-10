"""Runtime terminal outcome (Phase 23).

``RuntimeOutcome`` is the *terminal state* of one agent run — a small, stable
vocabulary that is deliberately **independent of ``RepairAction``**. It is the
contract consumed downstream by the API, the UI, background workers, HITL, and
the checkpoint store: they branch on the outcome (and, for waiting states, on
``pending_action``/``pending_reason``) without knowing anything about the
internal repair machinery.

This module only *derives and exposes* the terminal state. It never executes a
deferred repair. Deterministic and config-free: no LLM, no database, no settings.
"""

from enum import Enum

from app.agent.evaluation.models import RepairAction


class RuntimeOutcome(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNING = "completed_with_warning"
    FAILED = "failed"
    WAITING_FOR_CONTEXT = "waiting_for_context"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_REPLAN = "waiting_for_replan"


_WAITING = {
    RuntimeOutcome.WAITING_FOR_CONTEXT,
    RuntimeOutcome.WAITING_FOR_USER,
    RuntimeOutcome.WAITING_FOR_APPROVAL,
    RuntimeOutcome.WAITING_FOR_REPLAN,
}

# RepairAction → RuntimeOutcome for a failed evaluation's terminal repair.
# rerun_capability groups with retrieve_more_context: both mean "the runtime must
# produce more internal results/context before it can answer".
_ACTION_OUTCOME = {
    RepairAction.FAIL_GRACEFULLY: RuntimeOutcome.FAILED,
    RepairAction.RETURN_PARTIAL_WITH_WARNING: RuntimeOutcome.COMPLETED_WITH_WARNING,
    RepairAction.RETRIEVE_MORE_CONTEXT: RuntimeOutcome.WAITING_FOR_CONTEXT,
    RepairAction.RERUN_CAPABILITY: RuntimeOutcome.WAITING_FOR_CONTEXT,
    RepairAction.REPLAN: RuntimeOutcome.WAITING_FOR_REPLAN,
    RepairAction.ASK_USER_FOR_CLARIFICATION: RuntimeOutcome.WAITING_FOR_USER,
    RepairAction.HUMAN_REVIEW: RuntimeOutcome.WAITING_FOR_APPROVAL,
}


def derive_runtime_outcome(evaluator_ran: bool, report, terminal_repair):
    """Return ``(outcome, pending_action, pending_reason)`` for a finished run.

    - No evaluation, or a passing evaluation → ``COMPLETED``.
    - A failed evaluation maps by its terminal ``RepairResult``'s action.
    - ``pending_action``/``pending_reason`` are populated only for WAITING
      outcomes — the deferred stage a downstream consumer must drive next.
    """

    if not evaluator_ran or report is None or report.passed:
        return RuntimeOutcome.COMPLETED, None, None

    if terminal_repair is None:
        return RuntimeOutcome.COMPLETED_WITH_WARNING, None, report.reason or None

    action = terminal_repair.action
    # Exhausted/local regeneration that still failed → best-effort answer.
    outcome = _ACTION_OUTCOME.get(action, RuntimeOutcome.COMPLETED_WITH_WARNING)

    if outcome in _WAITING:
        return outcome, action.value, terminal_repair.reason or None
    if outcome == RuntimeOutcome.FAILED:
        return outcome, None, terminal_repair.reason or None
    return outcome, None, (terminal_repair.reason or report.reason or None)
