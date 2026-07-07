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

    # -- Document ingestion --------------------------------------------------
    max_upload_bytes: int = 25 * 1024 * 1024  # 25 MB
    allowed_content_types: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["application/pdf"]
    )
    chunk_size: int = 1000        # characters per chunk
    chunk_overlap: int = 150      # character overlap between chunks
    summary_max_chars: int = 1500  # cap on stub summary source text

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
