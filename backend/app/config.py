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
