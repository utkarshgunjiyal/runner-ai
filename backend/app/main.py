from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.logging_config import configure_logging, get_logger
from app.database import client, ensure_indexes
from app.http_middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.observability.metrics import NoOpMetrics, build_metrics_sink, configure_metrics
from app.rate_limit import InMemoryRateLimiter, RateLimits
from app.routes.health import router as health_router
from app.routes.chat import router as chat_router
from app.routes.documents import router as documents_router
from app.routes.jobs import router as jobs_router
from app.routes.memory import router as memory_router
from app.routes.agent import (
    router as agent_router,
    configure_checkpoint_store,
    configure_agent_runtime,
    configure_sse,
)
from app.agent.checkpoint.composition import select_checkpoint_store
from app.agent.checkpoint.mongo_store import mongo_collection_from_uri

configure_logging(settings.log_level)
logger = get_logger("app")

# -- Operational composition (Phase 42A) ------------------------------------ #
# Metrics sink: NoOp unless enabled; the backend (memory/prometheus) is selected
# here and installed process-wide. Rate limiter: Redis when configured, else an
# in-process fallback. Both are built once at import from settings.
_metrics = build_metrics_sink(settings.metrics_backend) if settings.metrics_enabled else NoOpMetrics()
configure_metrics(_metrics)


def _build_rate_limiter():
    if settings.rate_limit_enabled and settings.rate_limit_backend == "redis":
        try:
            from redis import asyncio as redis_asyncio

            from app.rate_limit import RedisRateLimiter

            return RedisRateLimiter(redis_asyncio.from_url(settings.redis_url))
        except Exception:  # noqa: BLE001 - degrade to in-process on any wiring error
            logger.warning("app.rate_limiter_redis_unavailable_falling_back")
    return InMemoryRateLimiter()


_rate_limiter = _build_rate_limiter()
_rate_limits = RateLimits(
    run=settings.rate_limit_run_per_minute,
    stream=settings.rate_limit_stream_per_minute,
    resume=settings.rate_limit_resume_per_minute,
)


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

    # Agent MCP transport (Phase 41A). Composition root owns the connection
    # lifecycle: it builds the connection manager + transport client, registers
    # and discovers TRUSTED server configs, and passes the pre-discovered manager
    # to the runtime. Default off (no servers) → the runtime is internal-only and
    # byte-identical. Route handlers are unchanged.
    mcp_connection_manager = None
    mcp_registry_manager = None
    if settings.agent_mcp_enabled:
        from app.agent.mcp.composition import build_mcp_registry_manager

        server_configs = load_trusted_mcp_server_configs()
        if server_configs:
            mcp_registry_manager, mcp_connection_manager = await build_mcp_registry_manager(
                server_configs
            )
            logger.info(
                "app.mcp_ready",
                extra={"servers": [c.public_metadata() for c in server_configs]},
            )

    # Agent LLM providers (Phase 37). Composition root selects deterministic vs
    # real V1.5-backed providers; routes stay config-free. One shared orchestrator.
    configure_agent_runtime(
        use_real_llm=settings.agent_use_real_llm,
        mcp_registry_manager=mcp_registry_manager,
    )
    logger.info("app.agent_llm_ready", extra={"use_real_llm": settings.agent_use_real_llm})

    # SSE keep-alive interval (Phase 42A). Routes stay config-free; set here.
    configure_sse(heartbeat_seconds=settings.sse_heartbeat_seconds)
    logger.info(
        "app.ops_ready",
        extra={
            "metrics_enabled": settings.metrics_enabled,
            "rate_limit_enabled": settings.rate_limit_enabled,
            "sse_heartbeat_seconds": settings.sse_heartbeat_seconds,
        },
    )

    yield

    if mcp_connection_manager is not None:
        await mcp_connection_manager.shutdown()  # graceful transport close (owned here)
    if checkpoint_mongo_client is not None:
        checkpoint_mongo_client.close()  # owned here; distinct from the Motor client
    client.close()
    logger.info("app.shutdown")


def load_trusted_mcp_server_configs():
    """Trusted MCP server configuration seam (Phase 41A).

    Returns the list of ``MCPServerConfig`` the composition root should mount.
    Empty by default — real deployments populate this from trusted configuration
    only (never from user request input). Kept as an explicit function so server
    registration stays out of route handlers and out of untrusted paths.
    """
    return []


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# Middleware stack (Phase 42A). Added inner→outer, so execution order is:
# CORS → RequestContext (correlation + metrics + logging) → SecurityHeaders →
# BodySizeLimit → RateLimit → routes. Rate-limit/body-limit rejections still get
# correlation headers, metrics, and CORS headers.
app.add_middleware(
    RateLimitMiddleware,
    enabled=settings.rate_limit_enabled,
    limiter=_rate_limiter,
    limits=_rate_limits,
    metrics=_metrics,
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
app.add_middleware(
    SecurityHeadersMiddleware,
    enabled=settings.security_headers_enabled,
    csp=settings.content_security_policy,
)
app.add_middleware(
    RequestContextMiddleware,
    header_name=settings.correlation_id_header,
    metrics=_metrics,
)

# Credentials cannot be combined with a wildcard origin per the CORS spec, so
# only enable them when explicit origins are configured. CORS is outermost.
_allow_all_origins = settings.cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[settings.correlation_id_header],
)


if settings.metrics_enabled:
    @app.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
        # Provider-neutral text exposition; only mounted when metrics are enabled.
        render = getattr(_metrics, "render_text", None)
        body = render() if callable(render) else ""
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


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
