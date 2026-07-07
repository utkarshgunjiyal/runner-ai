import os
import re
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import settings
from app.logging_config import get_logger
from app.schemas.document import DocumentPublic, DocumentStatus, UploadResponse
from app.services import (
    document_service,
    job_queue_service,
    job_service,
    storage_service,
)

logger = get_logger("documents")
router = APIRouter(prefix="/documents", tags=["documents"])

# Single-user placeholder until auth lands (Phase 5); matches chat_service.
DEV_USER_ID = "dev_user"


def _safe_filename(name: str | None) -> str:
    base = os.path.basename(name or "").strip() or "upload.pdf"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:200]


def _document_public(doc: dict) -> DocumentPublic:
    return DocumentPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        filename=doc["filename"],
        content_type=doc["content_type"],
        size_bytes=doc["size_bytes"],
        status=doc["status"],
        page_count=doc.get("page_count"),
        chunk_count=doc.get("chunk_count"),
        summary=doc.get("summary"),
        error=doc.get("error"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_document(file: UploadFile = File(...)) -> UploadResponse:
    user_id = DEV_USER_ID
    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()

    if content_type not in settings.allowed_content_types:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type '{content_type}'. "
            f"Allowed: {settings.allowed_content_types}",
        )

    data = await file.read()
    size_bytes = len(data)
    if size_bytes == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_bytes} bytes); max {settings.max_upload_bytes}",
        )

    filename = _safe_filename(file.filename)
    storage_key = f"{user_id}/{uuid.uuid4().hex}/{filename}"

    # Store raw bytes first; only then create records + enqueue so a failed
    # upload never leaves an orphaned job pointing at missing storage.
    await storage_service.put_object(storage_key, data, content_type)

    document = await document_service.create_document(
        user_id=user_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        storage_key=storage_key,
    )
    document_id = str(document["_id"])

    job = await job_service.create_job(user_id=user_id, document_id=document_id)
    job_id = str(job["_id"])

    await job_queue_service.enqueue_job(job_id)

    logger.info(
        "document.uploaded",
        extra={"document_id": document_id, "job_id": job_id, "size_bytes": size_bytes},
    )
    return UploadResponse(
        document_id=document_id,
        job_id=job_id,
        status=DocumentStatus.PENDING,
    )


@router.get("/{document_id}", response_model=DocumentPublic)
async def get_document_status(document_id: str) -> DocumentPublic:
    doc = await document_service.get_document(document_id, user_id=DEV_USER_ID)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _document_public(doc)
