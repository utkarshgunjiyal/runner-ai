from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class DocumentStatus(str, Enum):
    PENDING = "pending"          # stored, awaiting worker
    PROCESSING = "processing"    # worker running the pipeline
    COMPLETED = "completed"      # indexed + summarized
    FAILED = "failed"            # pipeline error (see error field)


class DocumentPublic(BaseModel):
    """API-facing view of a document record (ObjectId rendered as str)."""

    id: str
    user_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None = None
    chunk_count: int | None = None
    summary: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class UploadResponse(BaseModel):
    """Returned immediately from the upload endpoint (before processing)."""

    document_id: str
    job_id: str
    status: DocumentStatus
