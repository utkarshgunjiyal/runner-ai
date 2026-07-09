"""Execution result data models. See docs/architecture/v2.md §11-12."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"


class StepExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    capability_id: str | None = None
    status: StepStatus
    input: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    retry_count: int = 0
