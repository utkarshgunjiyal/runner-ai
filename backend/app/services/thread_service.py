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