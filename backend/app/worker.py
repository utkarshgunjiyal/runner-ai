"""Document ingestion worker.

Run as a separate process:  python -m app.worker

Blocking-pops job ids off the Redis queue and runs the ingestion pipeline for
each. Failures are already persisted (job + document marked failed) inside
``ingest_document``; the worker just logs and continues so one bad document
never takes the worker down.
"""

import asyncio
import signal

from app.config import settings
from app.logging_config import configure_logging, get_logger
from app.services import job_queue_service, storage_service, vector_store_service
from app.services.document_ingestion_service import ingest_document

logger = get_logger("worker")


async def _process(job_id: str) -> None:
    try:
        await ingest_document(job_id)
    except Exception:  # noqa: BLE001 - already logged/persisted; keep looping
        logger.error("worker.job_error", extra={"job_id": job_id})


async def _init_infra(retries: int = 30, delay: float = 2.0) -> None:
    """Ensure the MinIO bucket + Qdrant collection exist, tolerating infra that
    is still coming up (common on a fresh `docker compose up`)."""
    for attempt in range(1, retries + 1):
        try:
            await storage_service.ensure_bucket()
            await vector_store_service.ensure_collection()
            return
        except Exception:  # noqa: BLE001 - retry until infra is reachable
            if attempt == retries:
                raise
            logger.warning("worker.infra_wait", extra={"attempt": attempt})
            await asyncio.sleep(delay)


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    logger.info("worker.starting", extra={"queue": settings.job_queue_name})
    await _init_infra()
    logger.info("worker.ready")

    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            job_id = await job_queue_service.dequeue_job()
        except Exception:  # noqa: BLE001 - transient broker error; back off
            logger.exception("worker.dequeue_error")
            await asyncio.sleep(1)
            continue

        if job_id is None:
            continue  # dequeue timeout — loop and re-check stop_event

        logger.info("worker.job_received", extra={"job_id": job_id})
        await _process(job_id)

    logger.info("worker.stopped")


def main() -> None:
    configure_logging(settings.log_level)
    stop_event = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # e.g. non-main thread / unsupported platform
            pass

    try:
        loop.run_until_complete(run_worker(stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
