from datetime import datetime

from bson import ObjectId

from app.database import documents_collection
from app.schemas.document import DocumentStatus


async def create_document(
    user_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    storage_key: str,
    thread_id: str | None = None,
) -> dict:
    now = datetime.utcnow()
    doc = {
        "user_id": user_id,
        # Phase 43: documents belong to a (user, thread). thread_id is optional
        # for backward compatibility with legacy user-global documents.
        "thread_id": thread_id,
        "filename": filename,
        "normalized_filename": _normalize_filename(filename),
        "content_type": content_type,
        "size_bytes": size_bytes,
        "storage_key": storage_key,
        "status": DocumentStatus.PENDING.value,
        "page_count": None,
        "chunk_count": None,
        "summary": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await documents_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def _normalize_filename(name: str) -> str:
    import re

    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name or "")
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


async def list_thread_documents(user_id: str, thread_id: str, limit: int = 100) -> list[dict]:
    """Documents owned by (user, thread), newest first (Phase 43)."""
    cursor = (
        documents_collection.find({"user_id": user_id, "thread_id": thread_id})
        .sort("created_at", -1)
        .limit(max(1, min(limit, 500)))
    )
    return [doc async for doc in cursor]


async def get_document(document_id: str, user_id: str | None = None) -> dict | None:
    if not ObjectId.is_valid(document_id):
        return None
    query: dict = {"_id": ObjectId(document_id)}
    if user_id is not None:
        query["user_id"] = user_id
    return await documents_collection.find_one(query)


async def get_latest_completed_document(user_id: str) -> dict | None:
    """Most recently created COMPLETED document for a user.

    Used as the retrieval target when a request doesn't name a document_id.
    """
    return await documents_collection.find_one(
        {"user_id": user_id, "status": DocumentStatus.COMPLETED.value},
        sort=[("created_at", -1)],
    )


async def update_document(document_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": datetime.utcnow()}
    await documents_collection.update_one(
        {"_id": ObjectId(document_id)}, {"$set": fields}
    )


async def set_status(
    document_id: str,
    status: DocumentStatus,
    error: str | None = None,
) -> None:
    fields: dict = {"status": status.value}
    if error is not None:
        fields["error"] = error
    await update_document(document_id, fields)
