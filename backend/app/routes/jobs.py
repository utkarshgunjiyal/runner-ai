from fastapi import APIRouter, HTTPException

from app.schemas.job import JobPublic
from app.services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Single-user placeholder until auth lands (Phase 5).
DEV_USER_ID = "dev_user"


def _job_public(job: dict) -> JobPublic:
    return JobPublic(
        id=str(job["_id"]),
        user_id=job["user_id"],
        type=job["type"],
        document_id=job["document_id"],
        status=job["status"],
        attempts=job.get("attempts", 0),
        error=job.get("error"),
        result=job.get("result"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )


@router.get("/{job_id}", response_model=JobPublic)
async def get_job_status(job_id: str) -> JobPublic:
    job = await job_service.get_job(job_id, user_id=DEV_USER_ID)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_public(job)
