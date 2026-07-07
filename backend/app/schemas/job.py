from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class JobType(str, Enum):
    DOCUMENT_INGEST = "document_ingest"


class JobStatus(str, Enum):
    QUEUED = "queued"            # pushed to Redis, awaiting a worker
    PROCESSING = "processing"    # claimed by a worker
    COMPLETED = "completed"
    FAILED = "failed"


class JobPublic(BaseModel):
    """API-facing view of a job record."""

    id: str
    user_id: str
    type: JobType
    document_id: str
    status: JobStatus
    attempts: int = 0
    error: str | None = None
    result: dict | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
