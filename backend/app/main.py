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
    configure_run_recorder,
    configure_sse,
    get_current_user,
)
from app.routes.threads import router as threads_router
from app.routes.integrations import router as integrations_router
from app.deploy.startup_guard import check_startup_safety
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

    # Production auth/demo gate (Phase 42B). Refuse to boot an unsafe production
    # deployment: the dev auth stub silently authenticating everyone as
    # 'dev_user', or demo mode running in production. Dev auth is "active" unless
    # a real ``get_current_user`` has been installed via dependency override.
    dev_auth_active = get_current_user not in app.dependency_overrides
    problems = check_startup_safety(
        environment=settings.environment,
        dev_auth_active=dev_auth_active,
        allow_dev_auth=settings.allow_dev_auth,
        demo_mode=settings.demo_mode,
    )
    if problems:
        for problem in problems:
            logger.error("app.startup_refused", extra={"reason": problem})
        raise RuntimeError("Refusing to start: " + " | ".join(problems))

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
    mcp_result_normalizers = None
    # Phase 46.2: the GitHub read-only MCP connector reflects real health here. A
    # GitHub connection failure must NOT block startup — document/chat flows keep
    # working; GitHub simply reports its status truthfully.
    github_state = None
    server_configs = []
    if settings.agent_mcp_enabled:
        server_configs.extend(load_trusted_mcp_server_configs())
    if settings.github_mcp_ready:
        from app.agent.github import build_github_mcp_server_config

        server_configs.append(
            build_github_mcp_server_config(
                token=settings.resolved_github_token,
                image=settings.github_mcp_image,
                toolsets=settings.github_mcp_toolsets,
                timeout_seconds=settings.github_mcp_timeout_seconds,
            )
        )

    if server_configs:
        from app.agent.github import github_result_normalizer, github_spec_transform
        from app.agent.mcp.composition import build_mcp_registry_manager

        # Register without discovering, then discover each server best-effort so a
        # single server's failure cannot abort startup.
        mcp_registry_manager, mcp_connection_manager = await build_mcp_registry_manager(
            server_configs, spec_transform=github_spec_transform, discover=False
        )
        mcp_result_normalizers = {"github": github_result_normalizer}
        github_state = await _discover_mcp_servers(mcp_registry_manager, server_configs)
        logger.info(
            "app.mcp_ready",
            extra={"servers": [c.public_metadata() for c in server_configs]},
        )

    # Install the GitHub status provider for the integrations API + connector
    # eligibility (both read the same live state). Defaults to "not configured".
    from app.agent.github.status import derive_state
    from app.routes.integrations import configure_integrations

    if github_state is None:
        github_state = derive_state(configured=settings.github_mcp_enabled, connected=False,
                                    error_code="not_configured" if not settings.github_mcp_ready else None)
    _github_state_holder["state"] = github_state
    configure_integrations(lambda: _github_state_holder["state"])
    logger.info("app.github_ready", extra={"github_status": github_state.status})

    # Thread/document scope gate + run recorder (Phase 43). Composition root wires
    # the V1.5-backed callables; the runtime/routes stay config-free. The scope
    # gate resolves document references (and can pause for a genuine document
    # picker); the recorder validates thread ownership and persists messages.
    scope_gate = _build_scope_gate(github_state_fn=lambda: _github_state_holder["state"])
    document_inventory_fn = _build_document_inventory_fn()
    capability_executor = _build_capability_executor()
    configure_run_recorder(_build_run_recorder())

    # Agent LLM providers (Phase 37). Composition root selects deterministic vs
    # real V1.5-backed providers; routes stay config-free. One shared orchestrator.
    configure_agent_runtime(
        use_real_llm=settings.agent_use_real_llm,
        mcp_registry_manager=mcp_registry_manager,
        mcp_result_normalizers=mcp_result_normalizers,
        demo_mode=settings.demo_mode,
        scope_gate=scope_gate,
        document_inventory_fn=document_inventory_fn,
        connector_eligibility=True,
        capability_executor=capability_executor,
    )
    logger.info(
        "app.agent_llm_ready",
        extra={"use_real_llm": settings.agent_use_real_llm, "demo_mode": settings.demo_mode},
    )

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


def _build_scope_gate(*, github_state_fn=None):
    """Compose the Phase 43 ScopeGate from V1.5 services (lazy, composition-root)."""
    from app.agent.connectors import InMemoryConnectorRegistry
    from app.agent.documents import build_scoped_document_retriever
    from app.agent.github.status import build_github_connector_record
    from app.agent.runtime.scope_gate import ScopeGate
    from app.services import document_service

    async def thread_documents_fn(user_id, thread_id):
        docs = await document_service.list_thread_documents(user_id, thread_id)
        return [
            {
                "document_id": str(d["_id"]),
                "filename": d.get("filename"),
                "normalized_filename": d.get("normalized_filename"),
                "created_at": d.get("created_at"),
                "status": d.get("status"),
            }
            for d in docs
            # Only completed (indexed) documents are retrievable.
            if d.get("status") == "completed"
        ]

    async def recent_document_fn(user_id, thread_id):
        # Phase 44: "recent" must be a GENUINE prior-turn reference — the single
        # document a recent assistant turn actually resolved to — NOT "the newest
        # document in the thread" (that weak signal must never silently resolve a
        # vague reference when multiple documents exist). Returns None when the
        # immediate prior turns did not clearly reference exactly one document.
        from app.services import message_service

        messages = await message_service.get_recent_messages(
            user_id=user_id, thread_id=thread_id, limit=6
        )
        for message in reversed(messages):  # most recent first
            if message.get("role") != "assistant":
                continue
            resolved = (message.get("metadata") or {}).get("resolved_document_ids") or []
            if len(resolved) == 1:
                return str(resolved[0])
            # A turn that resolved to 0 or multiple docs is not a single reference.
            break
        return None

    # Connectors: the shipped registry is empty (no per-user OAuth yet). Phase 46.2
    # adds ONE deployment-scoped connector — GitHub — whose eligibility reflects the
    # real MCP health. When GitHub is not configured/connected, no github connector
    # is returned, so github capabilities are ineligible and never reach the planner.
    _registry = InMemoryConnectorRegistry()

    async def connectors_fn(user_id):
        records = await _registry.list_for_user(user_id)
        if github_state_fn is not None:
            record = build_github_connector_record(user_id, github_state_fn())
            if record is not None:
                records = [*records, record]
        return records

    return ScopeGate(
        thread_documents_fn=thread_documents_fn,
        document_retriever_fn=build_scoped_document_retriever(),
        recent_document_fn=recent_document_fn,
        connectors_fn=connectors_fn,
    )


def _build_document_inventory_fn():
    """Compose the Phase 46.1 document-inventory lister from the SAME V1.5 service
    the UI document selector uses (`document_service.list_thread_documents`), so the
    agent's inventory answer and the UI list derive from identical records. Scoped
    by user_id + thread_id; returns ALL statuses (Ready/Indexing/Failed) as safe
    fields only — never storage keys, chunk ids, or raw repository objects."""
    from app.services import document_service

    async def document_inventory_fn(user_id, thread_id):
        docs = await document_service.list_thread_documents(user_id, thread_id)
        return [
            {
                "document_id": str(d["_id"]),
                "filename": d.get("filename"),
                "status": d.get("status"),
                "created_at": d.get("created_at"),
            }
            for d in docs
        ]

    return document_inventory_fn


def _build_run_recorder():
    from app.services.agent_run_recorder import MongoRunRecorder

    return MongoRunRecorder()


def _build_capability_executor():
    """Wire the internal document capability to real retrieval (Phase 43 — this
    finally implements the Phase-13 DocumentAdapter TODO), so a planner that
    selects ``search_documents``/``get_document_summary`` executes real service
    calls instead of raising. Retrieval is filtered by user_id (+ the requested
    document scope); the ScopeGate remains the primary thread-scoped path."""
    from app.agent.documents import build_scoped_document_retriever
    from app.agent.execution.capability_executor import InternalCapabilityExecutor
    from app.agent.tools.internal.document_adapter import DocumentAdapter
    from app.services import document_service

    scoped = build_scoped_document_retriever()

    async def _retrieve(*, query, user_id, top_k=8, document_id=None, page=None):
        return await scoped(
            query=query, user_id=user_id, top_k=top_k,
            document_ids=[document_id] if document_id else None,
            pages=[page] if page else None,
        )

    async def _summary(*, document_id, user_id=None):
        doc = await document_service.get_document(document_id, user_id)
        return {"summary": (doc or {}).get("summary", "")}

    return InternalCapabilityExecutor(
        document_adapter=DocumentAdapter(retrieve_fn=_retrieve, summary_fn=_summary)
    )


def load_trusted_mcp_server_configs():
    """Trusted MCP server configuration seam (Phase 41A).

    Returns the list of ``MCPServerConfig`` the composition root should mount.
    Empty by default — real deployments populate this from trusted configuration
    only (never from user request input). Kept as an explicit function so server
    registration stays out of route handlers and out of untrusted paths.
    """
    return []


# Live GitHub connector state (Phase 46.2), set at startup and read by the
# integrations API + connector eligibility. A mutable holder so both readers see
# the same value without a global rebind.
_github_state_holder: dict = {"state": None}


async def _discover_mcp_servers(manager, configs):
    """Discover each registered MCP server best-effort and derive the GitHub state.

    A discovery failure for ANY server is caught and logged safely (no token/URL)
    so a single server cannot abort startup. Returns the GitHub ``GithubConnectorState``
    when a github server is present, else ``None``."""
    from app.agent.github.server import GITHUB_MCP_SERVER_ID
    from app.agent.github.status import derive_state
    from app.agent.mcp.errors import MCPError

    github_state = None
    for config in configs:
        try:
            specs = await manager.discover_server_tools(config.server_id)
        except MCPError as exc:
            logger.warning("app.mcp_discovery_failed", extra={
                "server_id": config.server_id, "error_code": getattr(exc, "error_code", "mcp_error")})
            if config.server_id == GITHUB_MCP_SERVER_ID:
                github_state = derive_state(configured=True, connected=False,
                                            error_code=getattr(exc, "error_code", "mcp_error"))
            continue
        except Exception:  # noqa: BLE001 - never leak; never abort startup
            logger.warning("app.mcp_discovery_error", extra={"server_id": config.server_id})
            if config.server_id == GITHUB_MCP_SERVER_ID:
                github_state = derive_state(configured=True, connected=False, error_code="mcp_error")
            continue
        if config.server_id == GITHUB_MCP_SERVER_ID:
            stats = manager.discovery_stats(config.server_id)
            github_state = derive_state(
                configured=True, connected=True,
                capabilities=[s.name for s in specs],
                discovered_tool_count=stats.get("discovered_tool_count", len(specs)),
                allowed_tool_count=stats.get("allowed_tool_count", len(specs)),
            )
    return github_state


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
app.include_router(threads_router)
app.include_router(integrations_router)


@app.get("/")
async def root():
    return {
        "message": f"{settings.app_name} backend is running",
        "version": settings.app_version,
    }
