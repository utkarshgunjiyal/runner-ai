"""Health endpoints (Phase 42A hardening).

- GET /health       — backward-compatible liveness+mongo summary (safe, no leak).
- GET /health/live  — process liveness (always 200 if the process is serving).
- GET /health/ready — readiness over required dependencies (Mongo/Redis/Qdrant/
  MinIO), 200 when all reachable, else 503. No error detail / credentials leak,
  and no paid LLM calls.
"""

import httpx
from fastapi import APIRouter, Response

from app.config import settings
from app.database import db
from app.health import liveness, run_readiness

router = APIRouter(prefix="/health", tags=["health"])


async def _check_mongo() -> bool:
    await db.command("ping")
    return True


async def _check_redis() -> bool:
    from redis import asyncio as redis_asyncio  # lazy

    client = redis_asyncio.from_url(settings.redis_url)
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def _check_qdrant() -> bool:
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
        return resp.status_code < 500


async def _check_minio() -> bool:
    scheme = "https" if settings.minio_secure else "http"
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(f"{scheme}://{settings.minio_endpoint}/minio/health/live")
        return resp.status_code < 500


def _readiness_checks() -> dict:
    return {
        "mongodb": _check_mongo,
        "redis": _check_redis,
        "qdrant": _check_qdrant,
        "minio": _check_minio,
    }


@router.get("")
async def health_check(response: Response):
    """Backward-compatible summary. Reports mongo connectivity without leaking
    error detail (a failure is a safe 503 status only)."""
    try:
        await db.command("ping")
        return {"status": "healthy", "services": {"mongodb": "connected"}}
    except Exception:  # noqa: BLE001 - never surface the raw error to clients
        response.status_code = 503
        return {"status": "unhealthy", "services": {"mongodb": "unavailable"}}


@router.get("/live")
async def health_live():
    return liveness()


@router.get("/ready")
async def health_ready(response: Response):
    report = await run_readiness(_readiness_checks())
    if report["status"] != "ready":
        response.status_code = 503
    return report
