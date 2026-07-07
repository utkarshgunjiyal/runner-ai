"""Object storage (MinIO) — stores raw uploaded files.

The MinIO SDK is synchronous, so blocking calls are offloaded to a thread to
keep the async request/worker paths non-blocking.
"""

import asyncio
import io

from minio import Minio

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("storage")

_client: Minio | None = None


def get_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _client


def _ensure_bucket_sync(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("storage.bucket_created", extra={"bucket": bucket})


async def ensure_bucket() -> None:
    client = get_client()
    await asyncio.to_thread(_ensure_bucket_sync, client, settings.minio_bucket)


def _put_sync(client: Minio, bucket: str, key: str, data: bytes, content_type: str) -> None:
    client.put_object(
        bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


async def put_object(key: str, data: bytes, content_type: str) -> None:
    client = get_client()
    await ensure_bucket()
    await asyncio.to_thread(_put_sync, client, settings.minio_bucket, key, data, content_type)
    logger.info("storage.put", extra={"key": key, "size_bytes": len(data)})


def _get_sync(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


async def get_object(key: str) -> bytes:
    client = get_client()
    return await asyncio.to_thread(_get_sync, client, settings.minio_bucket, key)
