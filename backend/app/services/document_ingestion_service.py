"""Document ingestion orchestrator.

Runs the full pipeline for one job: fetch bytes -> extract -> chunk -> embed
-> index in Qdrant -> summarize -> persist status. Called by the worker, but
kept independent of it so it can be invoked/tested directly.
"""

import asyncio

from app.logging_config import get_logger
from app.schemas.document import DocumentStatus
from app.services import (
    chunking_service,
    document_service,
    document_summary_service,
    embedding_service,
    job_service,
    pdf_service,
    storage_service,
    vector_store_service,
)

logger = get_logger("ingestion")


async def ingest_document(job_id: str) -> dict:
    job = await job_service.get_job(job_id)
    if not job:
        raise ValueError(f"job not found: {job_id}")

    document_id = job["document_id"]
    user_id = job["user_id"]

    await job_service.mark_processing(job_id)
    await document_service.set_status(document_id, DocumentStatus.PROCESSING)
    logger.info("ingest.start", extra={"job_id": job_id, "document_id": document_id})

    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise ValueError(f"document not found: {document_id}")

        # 1. Fetch the raw file from object storage.
        data = await storage_service.get_object(document["storage_key"])

        # 2. Extract text per page (blocking work -> thread).
        pages = await asyncio.to_thread(pdf_service.extract_pages, data)
        page_count = len(pages)

        # 3. Chunk with page provenance.
        chunks = chunking_service.chunk_pages(pages)
        chunk_count = len(chunks)

        # 4. Embed + 5. Index vectors in Qdrant.
        vectors_indexed = 0
        if chunks:
            provider = embedding_service.get_embedding_provider()
            vectors = await provider.embed([c["text"] for c in chunks])
            vectors_indexed = await vector_store_service.upsert_chunks(
                user_id=user_id,
                document_id=document_id,
                chunks=chunks,
                vectors=vectors,
                thread_id=document.get("thread_id"),
                filename=document.get("filename"),
            )

        # 6. Generate a document-level summary.
        summary = await document_summary_service.generate_document_summary(pages)

        # 7. Persist results.
        await document_service.update_document(
            document_id,
            {
                "status": DocumentStatus.COMPLETED.value,
                "page_count": page_count,
                "chunk_count": chunk_count,
                "summary": summary,
                "error": None,
            },
        )

        result = {
            "page_count": page_count,
            "chunk_count": chunk_count,
            "vectors_indexed": vectors_indexed,
        }
        await job_service.mark_completed(job_id, result)
        logger.info("ingest.completed", extra={"job_id": job_id, "document_id": document_id, **result})
        return result

    except Exception as exc:  # noqa: BLE001 - persist failure, then re-raise
        logger.exception("ingest.failed", extra={"job_id": job_id, "document_id": document_id})
        await document_service.set_status(document_id, DocumentStatus.FAILED, error=str(exc))
        await job_service.mark_failed(job_id, str(exc))
        raise
