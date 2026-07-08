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
) -> dict:
    now = datetime.utcnow()
    doc = {
        "user_id": user_id,
        "filename": filename,
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
