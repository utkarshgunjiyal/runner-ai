"""Run persistence seam (Phase 43).

A config-free boundary the agent routes call to (1) validate thread ownership and
persist the user message BEFORE a run, and (2) persist the assistant message +
run metadata and bump thread activity AFTER a run. The real implementation lives
in ``app.services.agent_run_recorder`` (V1.5-backed) and is installed at the
composition root; the default is no recorder, so unit tests stay byte-identical.
"""

from app.agent.persistence.run_recorder import (
    RunOutcomeView,
    RunRecorder,
    ThreadOwnershipError,
    outcome_view_from_result,
)

__all__ = [
    "RunRecorder",
    "RunOutcomeView",
    "ThreadOwnershipError",
    "outcome_view_from_result",
]
