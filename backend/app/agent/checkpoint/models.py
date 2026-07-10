"""Checkpoint models (Phase 24).

When a run reaches a WAITING_* RuntimeOutcome, its RunContext is snapshotted into
a ``CheckpointRecord`` so it can be resumed later (by a future resume flow, HITL,
or a background worker). Models only here — the store lives in ``store.py``.

Config-free: pydantic + the config-free RuntimeOutcome enum. No LLM, no database,
no application settings.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.runtime.outcome import RuntimeOutcome


class CheckpointStatus(str, Enum):
    ACTIVE = "active"
    RESUMED = "resumed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CheckpointRecord(BaseModel):
    """A persisted, resumable snapshot of a waiting run.

    Frozen — lifecycle transitions (resume/cancel/expire) produce a new record
    via ``model_copy`` rather than mutating in place.
    """

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str
    run_id: str
    user_id: str
    thread_id: str | None = None
    runtime_outcome: RuntimeOutcome
    pending_action: str | None = None
    pending_reason: str | None = None
    run_context_snapshot: dict = Field(default_factory=dict)
    status: CheckpointStatus = CheckpointStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    metadata: dict = Field(default_factory=dict)
