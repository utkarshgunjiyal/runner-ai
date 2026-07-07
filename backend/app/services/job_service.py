from datetime import datetime

from bson import ObjectId

from app.database import jobs_collection
from app.schemas.job import JobStatus, JobType


async def create_job(
    user_id: str,
    document_id: str,
    job_type: JobType = JobType.DOCUMENT_INGEST,
) -> dict:
    now = datetime.utcnow()
    job = {
        "user_id": user_id,
        "type": job_type.value,
        "document_id": document_id,
        "status": JobStatus.QUEUED.value,
        "attempts": 0,
        "error": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    result = await jobs_collection.insert_one(job)
    job["_id"] = result.inserted_id
    return job


async def get_job(job_id: str, user_id: str | None = None) -> dict | None:
    if not ObjectId.is_valid(job_id):
        return None
    query: dict = {"_id": ObjectId(job_id)}
    if user_id is not None:
        query["user_id"] = user_id
    return await jobs_collection.find_one(query)


async def mark_processing(job_id: str) -> None:
    now = datetime.utcnow()
    await jobs_collection.update_one(
        {"_id": ObjectId(job_id)},
        {
            "$set": {
                "status": JobStatus.PROCESSING.value,
                "started_at": now,
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
    )


async def mark_completed(job_id: str, result: dict) -> None:
    now = datetime.utcnow()
    await jobs_collection.update_one(
        {"_id": ObjectId(job_id)},
        {
            "$set": {
                "status": JobStatus.COMPLETED.value,
                "result": result,
                "error": None,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )


async def mark_failed(job_id: str, error: str) -> None:
    now = datetime.utcnow()
    await jobs_collection.update_one(
        {"_id": ObjectId(job_id)},
        {
            "$set": {
                "status": JobStatus.FAILED.value,
                "error": error,
                "finished_at": now,
                "updated_at": now,
            }
        },
    )
