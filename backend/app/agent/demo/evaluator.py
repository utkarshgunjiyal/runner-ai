"""DemoEvaluator — deterministic HITL trigger for the seeded demo (Phase 42B).

This implements the existing ``AnswerEvaluatorLike`` protocol
(``evaluate(final_prompt, final_answer, run_context=None) -> EvaluationReport``).
It is the SAME seam ``ScriptedEvaluator`` uses across the test suite; the only
new thing here is that the verdict is chosen from the *user request* by keyword
so a live demo is reproducible from the UI.

Behaviour:
- a request matching an approval keyword  → ``HUMAN_REVIEW`` → WAITING_FOR_APPROVAL
- a request matching a clarification keyword
  → ``ASK_USER_FOR_CLARIFICATION`` → WAITING_FOR_USER
- anything else → passes (``NONE``) → COMPLETED

The orchestrator derives the terminal ``RuntimeOutcome`` and pending action from
this report exactly as it does for any evaluator, so the pause is genuine:
real checkpoint, real ``/agent/resume``, real events. Config-free and
credential-free — pydantic only.
"""

from __future__ import annotations

from app.agent.evaluation.models import (
    EvaluationReport,
    RepairAction,
    RepairDecision,
)

DEFAULT_APPROVAL_KEYWORDS = (
    "delete",
    "deploy",
    "purchase",
    "send email",
    "approve",
)

DEFAULT_CLARIFICATION_KEYWORDS = (
    "summarize the report",
    "clarify",
    "ambiguous",
)

_APPROVAL_REASON = (
    "This action is high-impact and requires human approval before it proceeds."
)

_CLARIFICATION_REASON = (
    "The request is ambiguous; a clarification is needed to answer accurately."
)


def _request_text(final_prompt, run_context) -> str:
    """Safely extract the original user request for keyword matching."""
    text = getattr(run_context, "user_request", None)

    if isinstance(text, str) and text:
        return text

    for attr in ("user_request", "request", "query"):
        value = getattr(final_prompt, attr, None)

        if isinstance(value, str) and value:
            return value

    return ""


class DemoEvaluator:
    """Deterministic evaluator used only for the seeded demo scenario."""

    def __init__(
        self,
        *,
        approval_keywords: tuple[str, ...] = DEFAULT_APPROVAL_KEYWORDS,
        clarification_keywords: tuple[str, ...] = DEFAULT_CLARIFICATION_KEYWORDS,
    ) -> None:
        self._approval = tuple(
            keyword.lower() for keyword in approval_keywords
        )
        self._clarification = tuple(
            keyword.lower() for keyword in clarification_keywords
        )

    def evaluate(
        self,
        final_prompt,
        final_answer,
        run_context=None,
    ) -> EvaluationReport:
        metadata = getattr(run_context, "metadata", {}) if run_context else {}
        resume = metadata.get("resume", {})
        resume_kind = resume.get("kind")

        if resume_kind in {"approval", "clarification"}:
            return EvaluationReport(
                passed=True,
                overall_score=1.0,
                reason=f"demo: resumed with {resume_kind}; answer accepted",
                repair_decision=RepairDecision(
                    action=RepairAction.NONE,
                ),
                metadata={
                    "demo_mode": True,
                    "resumed": True,
                    "resume_kind": resume_kind,
                },
            )


        request = _request_text(final_prompt, run_context).lower()

        if any(keyword in request for keyword in self._approval):
            return self._pause(
                RepairAction.HUMAN_REVIEW,
                _APPROVAL_REASON,
            )

        if any(keyword in request for keyword in self._clarification):
            return self._pause(
                RepairAction.ASK_USER_FOR_CLARIFICATION,
                _CLARIFICATION_REASON,
            )

        return EvaluationReport(
            passed=True,
            overall_score=1.0,
            reason="demo: no trigger matched; answer accepted",
            repair_decision=RepairDecision(
                action=RepairAction.NONE,
            ),
            metadata={
                "demo_mode": True,
            },
        )

    @staticmethod
    def _pause(
        action: RepairAction,
        reason: str,
    ) -> EvaluationReport:
        return EvaluationReport(
            passed=False,
            overall_score=0.2,
            reason=reason,
            repair_decision=RepairDecision(
                action=action,
                reason=reason,
                max_attempts=1,
            ),
            metadata={
                "demo_mode": True,
            },
        )