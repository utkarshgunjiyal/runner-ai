from functools import lru_cache
from pathlib import Path
from typing import Annotated, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root (…/runner-ai). Kept identical to the pre-Phase-0 behaviour so the
# same top-level .env file continues to be loaded.
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables / .env.

    Unknown variables are ignored (``extra="ignore"``) so the shared .env can
    carry configuration for later phases (Redis, Qdrant, MinIO, LLM keys)
    without breaking startup today.
    """

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application ---------------------------------------------------------
    app_name: str = "Runner.ai"
    app_version: str = "1.5.0"
    environment: str = "development"
    log_level: str = "INFO"

    # -- MongoDB -------------------------------------------------------------
    mongo_url: str = Field(..., description="MongoDB connection string")
    db_name: str = "runner_ai_v1"

    # -- Agent checkpoint store (Phase 35) -----------------------------------
    # "memory" (default) uses the in-process InMemoryCheckpointStore; "mongo"
    # selects the durable MongoCheckpointStore, reusing mongo_url/db_name above.
    agent_checkpoint_backend: str = "memory"
    agent_checkpoint_collection: str = "agent_checkpoints"

    # -- Agent LLM providers (Phase 37) --------------------------------------
    # False (default) uses the deterministic providers (safe for tests/local);
    # True selects the V1.5-backed planner/final adapters, reusing the existing
    # llm_provider/llm_model settings above. No second LLM config system.
    agent_use_real_llm: bool = False

    # -- Agent MCP transport (Phase 41A) -------------------------------------
    # False (default) = no MCP servers; the runtime is internal-only and
    # byte-identical. When True, the composition root builds the connection
    # manager + transport client and mounts trusted MCP server configs. Server
    # configuration comes from trusted composition only (never user input).
    agent_mcp_enabled: bool = False

    # -- GitHub read-only MCP connector (Phase 46.2) -------------------------
    # Deployment-scoped GitHub integration through the official GitHub MCP server.
    # Disabled unless BOTH a flag and a token are present. The token is a SECRET —
    # read from the environment only, never committed, printed, logged, returned to
    # the frontend, or placed in a ToolSpec/metadata. True multi-user OAuth is NOT
    # implemented: the configured identity is shared by the deployment, so access to
    # a deployment with this enabled must be restricted (see docs/SECURITY.md).
    github_mcp_enabled: bool = False
    # Accepts either GITHUB_MCP_TOKEN or the server's native GITHUB_PERSONAL_ACCESS_TOKEN.
    github_mcp_token: str | None = Field(
        default=None,
        validation_alias="github_mcp_token",
        repr=False,
    )
    github_personal_access_token: str | None = Field(default=None, repr=False)
    # Transport (Phase 46.2.1): "http" (default) uses the official remote Streamable
    # HTTP MCP endpoint — works from a containerized backend over outbound HTTPS with
    # NO Docker socket, NO Docker CLI, and NO Docker-in-Docker. "stdio" is an optional
    # developer mode that launches the server as a local Docker process (the host
    # running Runner.ai must have Docker available; not for Compose).
    github_mcp_transport: str = "http"
    # Remote MCP endpoint (http mode only). The token goes in the Authorization
    # header, never in the URL.
    github_mcp_url: str = "https://api.githubcopilot.com/mcp/"
    # Pinned image reference (stdio mode only; never a floating ``latest``).
    github_mcp_image: str = "ghcr.io/github/github-mcp-server:v0.6.0"
    github_mcp_toolsets: str = "repos,issues,pull_requests"
    github_mcp_timeout_seconds: float = 45.0
    # Optional deployment-scoped authenticated GitHub owner/login (Phase 46.2.6).
    # Used to scope account requests ("my repositories", "my <repo>") when the
    # remote identity cannot be resolved. Deployment-scoped, NOT per-user OAuth; a
    # public handle, never a secret. Left unset → identity is resolved best-effort
    # from the connector, else account-scoped requests clarify rather than guess.
    github_mcp_owner: str | None = None

    @field_validator("github_mcp_transport")
    @classmethod
    def _validate_github_transport(cls, value: str) -> str:
        allowed = {"http", "stdio"}
        normalized = (value or "http").strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"github_mcp_transport must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalized

    @property
    def resolved_github_token(self) -> str | None:
        """The GitHub token from either accepted env var (secret; never logged)."""
        token = self.github_mcp_token or self.github_personal_access_token
        return token.strip() if token and token.strip() else None

    @property
    def github_mcp_ready(self) -> bool:
        """True only when GitHub is enabled AND a token is present AND — for http
        mode — a URL is configured (fail-safe)."""
        if not (self.github_mcp_enabled and self.resolved_github_token):
            return False
        if self.github_mcp_transport == "http" and not (self.github_mcp_url or "").strip():
            return False
        return True

    @field_validator("agent_checkpoint_backend")
    @classmethod
    def _validate_checkpoint_backend(cls, value: str) -> str:
        allowed = {"memory", "mongo"}
        normalized = (value or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"agent_checkpoint_backend must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalized

    # -- Redis (job queue) ---------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    job_queue_name: str = "runner:jobs:document_ingest"
    worker_dequeue_timeout: int = 5  # seconds for blocking pop

    # -- MinIO / object storage ----------------------------------------------
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "runner-uploads"
    minio_secure: bool = False

    # -- Qdrant / vector store -----------------------------------------------
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "document_chunks"

    # -- Embeddings ----------------------------------------------------------
    # A deterministic stub provider is used until a real model is wired in
    # (Phase 2/3). embedding_dim is the vector size stored in Qdrant.
    embedding_dim: int = 384

    # -- LLM (Phase 3) -------------------------------------------------------
    # provider: "auto" picks anthropic if ANTHROPIC_API_KEY is set, else
    # openrouter if OPENROUTER_API_KEY is set, else a no-network "stub" so the
    # app still runs without credentials. Set explicitly to override.
    llm_provider: str = "auto"  # auto | anthropic | openrouter | stub
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.3
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2  # extra attempts on transient failures

    # -- Document ingestion --------------------------------------------------
    max_upload_bytes: int = 25 * 1024 * 1024  # 25 MB
    allowed_content_types: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["application/pdf"]
    )
    chunk_size: int = 1000        # characters per chunk
    chunk_overlap: int = 150      # character overlap between chunks
    summary_max_chars: int = 1500  # cap on stub summary source text

    # -- Context budget (Phase 4) --------------------------------------------
    # Rough chars-per-token ratio used to enforce ContextPolicy.context_budget_tokens
    # when assembling evidence. Lower = more conservative (fewer chars per token).
    context_chars_per_token: int = 4

    # -- Operational hardening (Phase 42A) -----------------------------------
    # All operational features below are additive and default to a safe/off
    # posture so the default test suite and dev workflow are unchanged.

    # Request correlation. A client-supplied value in this header is honored only
    # when it passes validation (safe charset, bounded length); otherwise a fresh
    # id is generated. Independent of the runtime's run_id.
    correlation_id_header: str = "X-Request-ID"
    # Reject request bodies larger than this (safety ceiling; > max_upload_bytes so
    # document uploads still work). 413 on exceed.
    max_request_body_bytes: int = 32 * 1024 * 1024

    # Security response headers (safe for an API; the SPA sets its own CSP in nginx).
    security_headers_enabled: bool = True
    content_security_policy: str = "default-src 'none'; frame-ancestors 'none'"

    # Metrics. Provider-neutral by default (in-memory, no endpoint). Set
    # metrics_enabled=True to expose GET /metrics; metrics_backend selects the
    # adapter ("memory" text or "prometheus" when prometheus_client is installed).
    metrics_enabled: bool = False
    metrics_backend: str = "memory"  # memory | prometheus

    # Rate limiting. Off by default (dev/tests unchanged). When enabled, applies
    # to /agent/run, /agent/run/stream and /agent/resume, keyed by user (falling
    # back to client host). "redis" is required for a real multi-process limit;
    # "memory" is a per-process fallback for local use only.
    rate_limit_enabled: bool = False
    rate_limit_backend: str = "memory"  # memory | redis
    rate_limit_run_per_minute: int = 30
    rate_limit_stream_per_minute: int = 10
    rate_limit_resume_per_minute: int = 60

    # SSE keep-alive: emit a heartbeat comment after this many idle seconds so
    # proxies do not close an idle stream. 0 disables heartbeats.
    sse_heartbeat_seconds: float = 15.0

    # Opt-in sensitive logging (prompts, payloads). OFF by default and never
    # enabled in production; a guard for local debugging only.
    log_sensitive: bool = False

    # ----------------------------------------------------------------------- #
    # Deployment & demo gates (Phase 42B). All additive, safe/off by default,
    # so the default test suite and dev workflow stay byte-identical.
    # ----------------------------------------------------------------------- #

    # Demo mode. OFF by default and never on in production. When true, the
    # composition root wires a DemoEvaluator (the EXISTING answer-evaluator seam)
    # so marked demo prompts deterministically reach a genuine HITL pause
    # (WAITING_FOR_APPROVAL / WAITING_FOR_USER) that flows through the real
    # orchestrator → checkpoint → /agent/resume. It never bypasses the runtime
    # state machine and never fabricates events.
    demo_mode: bool = False

    # Production auth gate (Phase 42B). The API ships a development auth stub that
    # authenticates everyone as ``dev_user``. In production the app refuses to
    # start while that stub is active UNLESS this is explicitly set true (e.g. a
    # private demo behind reverse-proxy basic auth). Real multi-user deployments
    # replace the stub and leave this false. See docs/SECURITY.md.
    allow_dev_auth: bool = False

    # Cookie policy expectation for production (documentation/enforcement hook for
    # real auth). The dev stub sets no cookies; a real ``get_current_user`` should
    # honor these. Kept here so env validation can assert a secure posture.
    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # lax | strict | none

    @field_validator("cookie_samesite")
    @classmethod
    def _validate_cookie_samesite(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError(
                f"cookie_samesite must be 'lax', 'strict' or 'none', got {value!r}"
            )
        return normalized

    @field_validator("metrics_backend")
    @classmethod
    def _validate_metrics_backend(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"memory", "prometheus"}:
            raise ValueError(f"metrics_backend must be 'memory' or 'prometheus', got {value!r}")
        return normalized

    @field_validator("rate_limit_backend")
    @classmethod
    def _validate_rate_limit_backend(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"memory", "redis"}:
            raise ValueError(f"rate_limit_backend must be 'memory' or 'redis', got {value!r}")
        return normalized

    @field_validator("allowed_content_types", mode="before")
    @classmethod
    def _parse_content_types(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ["application/pdf"]
            return [ct.strip() for ct in value.split(",") if ct.strip()]
        return value

    # -- CORS ----------------------------------------------------------------
    # Comma-separated string in the env var (e.g. "http://a.com,http://b.com").
    # NoDecode stops pydantic-settings from JSON-parsing the value so the
    # validator below receives the raw string.
    cors_origins: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["*"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ["*"]
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Import-time singleton. Fails fast (ValidationError) if MONGO_URL is missing,
# preserving the previous "MONGO_URL is missing" startup guard.
settings = get_settings()
