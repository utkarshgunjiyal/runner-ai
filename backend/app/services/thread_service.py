from datetime import datetime
from bson import ObjectId
from fastapi import HTTPException

from app.database import threads_collection
from pymongo import ReturnDocument


async def create_thread(user_id: str, title: str) -> dict:
    now = datetime.utcnow()

    thread_doc = {
        "user_id": user_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "last_message_seq": 0,
    }

    result = await threads_collection.insert_one(thread_doc)
    thread_doc["_id"] = result.inserted_id

    return thread_doc


async def list_threads(user_id: str, limit: int = 50) -> list[dict]:
    """The user's threads, most-recently-active first (Phase 43)."""
    cursor = (
        threads_collection.find({"user_id": user_id})
        .sort("updated_at", -1)
        .limit(max(1, min(limit, 200)))
    )
    return [thread async for thread in cursor]


async def update_thread_title(user_id: str, thread_id: str, title: str) -> dict:
    """Rename a thread the user owns (Phase 43). 404 if not owned."""
    if not ObjectId.is_valid(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id")
    updated = await threads_collection.find_one_and_update(
        {"_id": ObjectId(thread_id), "user_id": user_id},
        {"$set": {"title": title, "updated_at": datetime.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Thread not found")
    return updated


async def touch_thread(user_id: str, thread_id: str) -> None:
    """Bump a thread's activity timestamp (Phase 43). Safe no-op if not owned."""
    if not ObjectId.is_valid(thread_id):
        return
    await threads_collection.update_one(
        {"_id": ObjectId(thread_id), "user_id": user_id},
        {"$set": {"updated_at": datetime.utcnow()}},
    )


async def get_thread(user_id: str, thread_id: str) -> dict:
    if not ObjectId.is_valid(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id")

    thread = await threads_collection.find_one({
        "_id": ObjectId(thread_id),
        "user_id": user_id,
    })

    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    return thread


async def allocate_next_sequence(user_id: str, thread_id: str) -> int:
    updated_thread = await threads_collection.find_one_and_update(
        {
            "_id": ObjectId(thread_id),
            "user_id": user_id,
        },
        {
            "$inc": {
                "last_message_seq": 1,
                "message_count": 1,
            },
            "$set": {
                "updated_at": datetime.utcnow(),
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    return updated_thread["last_message_seq"]