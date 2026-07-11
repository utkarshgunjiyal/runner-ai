"""RunRecorder protocol + a safe run-outcome view (Phase 43). Config-free."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ThreadOwnershipError(Exception):
    """Raised when a thread_id does not belong to the authenticated user."""


class RunOutcomeView(BaseModel):
    """A SAFE, transport-agnostic view of a finished run for persistence.

    Built identically from an AgentRunResult (`/agent/run`) or from the terminal
    stream events (`/agent/run/stream`), so the recorder sees one shape. Carries
    no internal RunContext/FinalPrompt and never a raw stack trace."""

    model_config = ConfigDict(frozen=True)

    run_id: str | None = None
    runtime_outcome: str = "completed"
    answer_text: str | None = None
    pending_action: str | None = None
    pending_reason: str | None = None
    checkpoint_id: str | None = None
    resolved_document_ids: list[str] = Field(default_factory=list)

    @property
    def is_waiting(self) -> bool:
        return self.runtime_outcome.startswith("waiting_")

    @property
    def is_failed(self) -> bool:
        return self.runtime_outcome == "failed"


def outcome_view_from_result(result, checkpoint_id: str | None = None) -> RunOutcomeView:
    outcome = getattr(result.runtime_outcome, "value", result.runtime_outcome)
    scope = (getattr(result, "metadata", {}) or {}).get("document_scope") or {}
    resolved = scope.get("resolved_document_ids") if isinstance(scope, dict) else None
    # A waiting/failed run has no user-facing answer to persist as assistant text.
    answer_text = None
    if outcome in ("completed", "completed_with_warning"):
        answer_text = getattr(getattr(result, "answer", None), "text", None)
    return RunOutcomeView(
        run_id=getattr(result, "run_id", None),
        runtime_outcome=str(outcome),
        answer_text=answer_text,
        pending_action=getattr(result, "pending_action", None),
        pending_reason=getattr(result, "pending_reason", None),
        checkpoint_id=checkpoint_id,
        resolved_document_ids=list(resolved or []),
    )


@runtime_checkable
class RunRecorder(Protocol):
    async def before_run(
        self, user_id: str, thread_id: str | None, user_request: str
    ) -> str | None:
        """Validate thread ownership and persist the user message. Returns the
        effective thread_id (possibly newly created). Raises ThreadOwnershipError
        if thread_id is not owned by user_id."""
        ...

    async def after_run(
        self, user_id: str, thread_id: str | None, outcome: RunOutcomeView
    ) -> None:
        """Persist the assistant message + safe run metadata and bump thread
        activity. Waiting/failed runs persist a safe pending/failure state; raw
        stack traces are never persisted as assistant content."""
        ...
