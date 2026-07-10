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
