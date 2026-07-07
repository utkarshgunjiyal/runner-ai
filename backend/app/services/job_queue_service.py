"""Redis-backed job queue.

The queue carries only job ids; the authoritative job record lives in MongoDB.
Producers RPUSH; the worker BLPOPs (blocking) so it sleeps until work arrives.
"""

import redis.asyncio as redis

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("job_queue")

_redis: "redis.Redis | None" = None


def get_redis() -> "redis.Redis":
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def enqueue_job(job_id: str) -> None:
    await get_redis().rpush(settings.job_queue_name, job_id)
    logger.info("job.enqueued", extra={"job_id": job_id, "queue": settings.job_queue_name})


async def dequeue_job(timeout: int | None = None) -> str | None:
    """Blocking pop of the next job id, or None when the timeout elapses."""
    timeout = settings.worker_dequeue_timeout if timeout is None else timeout
    result = await get_redis().blpop(settings.job_queue_name, timeout=timeout)
    if result is None:
        return None
    _, job_id = result
    return job_id
