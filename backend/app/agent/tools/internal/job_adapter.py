"""Internal job adapter (Phase 13).

Bridges ``jobs.get_status`` to the V1.5 job service. The V1.5 signature
(``job_service.get_job(job_id, user_id)``) maps cleanly, so the real service
call is lazily wired here; tests inject a fake instead.
"""

from app.agent.tools.internal.base import InternalAdapter
from app.agent.tools.result import AdapterResult, ErrorCode


class JobAdapter(InternalAdapter):
    name = "jobs"

    GET_STATUS = "jobs.get_status"

    def __init__(self, get_job_fn=None) -> None:
        self._get_job_fn = get_job_fn

    def _handlers(self):
        return {self.GET_STATUS: self._get_status}

    async def _resolve_get_job(self):
        if self._get_job_fn is None:
            from app.services.job_service import get_job

            self._get_job_fn = get_job
        return self._get_job_fn

    async def _get_status(self, args: dict) -> AdapterResult:
        job_id = args.get("job_id")
        if not job_id:
            return AdapterResult.failure(
                ErrorCode.INVALID_ARGS,
                metadata={"missing": "job_id is required"},
            )

        get_job = await self._resolve_get_job()
        job = await get_job(job_id=job_id, user_id=args.get("user_id"))
        if not job:
            return AdapterResult.failure(
                ErrorCode.NOT_FOUND,
                metadata={"job_id": job_id},
            )
        return AdapterResult.ok(
            output={"job": job, "status": job.get("status")},
            metadata={"job_id": job_id},
        )
