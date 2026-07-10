import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logging_config import configure_logging, get_logger, request_id_ctx
from app.database import client, ensure_indexes
from app.routes.health import router as health_router
from app.routes.chat import router as chat_router
from app.routes.documents import router as documents_router
from app.routes.jobs import router as jobs_router
from app.routes.memory import router as memory_router
from app.routes.agent import (
    router as agent_router,
    configure_checkpoint_store,
    configure_agent_runtime,
)
from app.agent.checkpoint.composition import select_checkpoint_store
from app.agent.checkpoint.mongo_store import mongo_collection_from_uri

configure_logging(settings.log_level)
logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "app.startup",
        extra={
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
        },
    )
    await ensure_indexes()
    logger.info("app.indexes_ready")

    # Agent checkpoint backend (Phase 35). Composition root reads config and
    # selects/wires the shared store; routes stay config-free. Mongo backend
    # builds its own SYNCHRONOUS pymongo client (the checkpoint store is sync);
    # we own and close it here.
    checkpoint_mongo_client = None
    if settings.agent_checkpoint_backend == "mongo":
        collection = mongo_collection_from_uri(
            settings.mongo_url, settings.db_name, settings.agent_checkpoint_collection
        )
        checkpoint_mongo_client = collection.database.client
        store = select_checkpoint_store("mongo", mongo_collection=collection)
    else:
        store = select_checkpoint_store(settings.agent_checkpoint_backend)
    configure_checkpoint_store(store)
    logger.info("app.checkpoint_backend_ready", extra={"backend": settings.agent_checkpoint_backend})

    # Agent LLM providers (Phase 37). Composition root selects deterministic vs
    # real V1.5-backed providers; routes stay config-free. One shared orchestrator.
    configure_agent_runtime(use_real_llm=settings.agent_use_real_llm)
    logger.info("app.agent_llm_ready", extra={"use_real_llm": settings.agent_use_real_llm})

    yield

    if checkpoint_mongo_client is not None:
        checkpoint_mongo_client.close()  # owned here; distinct from the Motor client
    client.close()
    logger.info("app.shutdown")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# Credentials cannot be combined with a wildcard origin per the CORS spec, so
# only enable them when explicit origins are configured.
_allow_all_origins = settings.cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_ctx.set(request_id)
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "request.failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        request_id_ctx.reset(token)
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "request.completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    response.headers["X-Request-ID"] = request_id
    request_id_ctx.reset(token)
    return response


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(jobs_router)
app.include_router(memory_router)
app.include_router(agent_router)


@app.get("/")
async def root():
    return {
        "message": f"{settings.app_name} backend is running",
        "version": settings.app_version,
    }
