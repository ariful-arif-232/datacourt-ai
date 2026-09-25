"""Environment configuration, validated at startup.

Every deployment-specific value comes from the environment. Nothing here has a
production default for secrets: the app refuses to start in production without them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Aliased settings are read only under their explicit names (e.g. DATACOURT_GITHUB_TOKEN):
    # never under the bare field name, which would pick up ambient variables such as the
    # GITHUB_TOKEN / GITHUB_REF / GITHUB_WORKFLOW that CI systems set.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5432/datacourt"

    # Auth / signing
    auth_secret: str = Field(default="dev-insecure-auth-secret-change-me-0123456789")
    signed_url_secret: str = Field(default="dev-insecure-signing-secret-change-me-0123")
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool = False
    cookie_domain: str | None = None
    allowed_origins: str = "http://localhost:3000"
    public_app_url: str = "http://localhost:3000"
    public_api_url: str = "http://localhost:8000"

    # Storage. "s3" works with any S3-compatible service; production uses Backblaze B2
    # (S3_ENDPOINT_URL=https://s3.<region>.backblazeb2.com, S3_REGION=<region>).
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_root: str = "./.data/objects"
    s3_endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_ENDPOINT_URL", "S3_ENDPOINT")
    )
    s3_bucket: str | None = Field(default=None, validation_alias=AliasChoices("S3_BUCKET"))
    s3_access_key_id: str | None = Field(default=None, validation_alias=AliasChoices("S3_ACCESS_KEY_ID"))
    s3_secret_access_key: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_SECRET_ACCESS_KEY")
    )
    s3_region: str = Field(default="us-east-1", validation_alias=AliasChoices("S3_REGION"))
    s3_addressing_style: Literal["path", "virtual"] = Field(
        default="path", validation_alias=AliasChoices("S3_ADDRESSING_STYLE")
    )
    # Uploads larger than this use presigned multipart uploads (resumable per part).
    s3_multipart_threshold_bytes: int = 64 * 1024**2
    s3_multipart_part_bytes: int = 16 * 1024**2
    signed_url_ttl_seconds: int = 900
    upload_url_ttl_seconds: int = 6 * 3600
    # Thumbnails are served through a CDN-cacheable capability URL (valid for ~1-2 weeks) so that
    # browsing does not spend the object store's per-download quota on every page view.
    media_url_period_seconds: int = 7 * 24 * 3600
    # Worker-side write-through cache for object bytes (keeps storage GETs to ~1 per dataset).
    object_cache_dir: str | None = None
    object_cache_max_bytes: int = 8 * 1024**3

    # Limits
    max_upload_bytes: int = 2 * 1024**3
    max_extracted_bytes: int = 8 * 1024**3
    max_files_per_archive: int = 200_000
    max_image_pixels: int = 64_000_000
    thumbnail_size: int = 256

    # ML
    embedding_backend: Literal["dc-descriptor-v1", "dinov2-small"] = "dc-descriptor-v1"
    model_cache_dir: str = "./.data/models"

    # Gemini (optional)
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0

    # Role of this process: "api" (web/serverless) or "worker" (batch jobs). Controls which
    # production settings are required.
    role: Literal["api", "worker"] = Field(default="api", validation_alias=AliasChoices("DATACOURT_ROLE"))

    # Execution backend: how the API gets queued jobs processed.
    #   embedded       - worker thread inside the API process (local development)
    #   external       - a separately running `datacourt-worker` polls the queue
    #   github_actions - the API dispatches the DataCourt worker workflow on GitHub Actions
    #   none           - never dispatch (tests, and inside the worker itself)
    execution_backend: Literal["embedded", "external", "github_actions", "none"] = "embedded"
    github_api_url: str = Field(
        default="https://api.github.com", validation_alias=AliasChoices("DATACOURT_GITHUB_API_URL")
    )
    github_repository: str | None = Field(
        default=None, validation_alias=AliasChoices("DATACOURT_GITHUB_REPO")
    )
    github_workflow: str = Field(
        default="datacourt-worker.yml", validation_alias=AliasChoices("DATACOURT_GITHUB_WORKFLOW")
    )
    # Branch whose workflow file and code the worker runs; defaults to the branch Vercel deployed.
    github_ref: str | None = Field(default=None, validation_alias=AliasChoices("DATACOURT_GITHUB_REF"))
    github_token: str | None = Field(default=None, validation_alias=AliasChoices("DATACOURT_GITHUB_TOKEN"))
    worker_max_parallel: int = Field(default=2, ge=1, le=10)
    # Re-dispatch a queued job whose runner has not picked it up after this long.
    dispatch_retry_after_seconds: int = 180

    # Worker
    run_embedded_worker: bool = False
    worker_poll_seconds: float = 1.0
    job_heartbeat_seconds: float = 10.0
    job_stale_after_seconds: float = 180.0
    retention_sweep_seconds: float = 3600.0
    # Logs of a public repository's Actions runs are public: log ids, types and timings only.
    public_logs: bool = False

    # Rate limiting (requests per minute per client key)
    rate_limit_auth_per_minute: int = 20
    rate_limit_default_per_minute: int = 600

    # Demo workspace
    demo_enabled: bool = True
    seed_demo_on_start: bool = False

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Neon and most providers hand out postgres:// URLs; SQLAlchemy needs the driver named.
        if v.startswith("postgres://"):
            v = "postgresql+psycopg://" + v[len("postgres://") :]
        elif v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        # A Vercel deployment is always held to production rules: a forgotten ENVIRONMENT must not
        # fall back to development defaults (public dev secrets, insecure cookies, local storage).
        if self.environment == "production" or self.on_vercel:
            problems = self.production_problems()
            if problems:
                raise ValueError("; ".join(problems))
        return self

    def production_problems(self) -> list[str]:
        """Missing or unsafe production settings (names only, never values)."""
        problems: list[str] = []
        if self.on_vercel and self.environment != "production":
            problems.append("ENVIRONMENT must be production on Vercel")
        if "127.0.0.1" in self.database_url or "localhost" in self.database_url:
            problems.append("DATABASE_URL must point at the production database")
        if self.storage_backend != "s3":
            problems.append("STORAGE_BACKEND must be s3 in production")
        elif not all([self.s3_endpoint, self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key]):
            problems.append(
                "S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY are required"
            )
        if self.role == "api":
            if "dev-insecure" in self.auth_secret or len(self.auth_secret) < 32:
                problems.append("AUTH_SECRET must be a random value of at least 32 characters")
            if "dev-insecure" in self.signed_url_secret or len(self.signed_url_secret) < 32:
                problems.append("SIGNED_URL_SECRET must be a random value of at least 32 characters")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true in production")
            if self.execution_backend == "github_actions" and not (
                self.github_token and self.github_repository
            ):
                problems.append(
                    "DATACOURT_GITHUB_TOKEN and DATACOURT_GITHUB_REPO are required for github_actions"
                )
            if self.execution_backend == "embedded" and self.on_vercel:
                problems.append("EXECUTION_BACKEND must be github_actions or external on Vercel")
        return problems

    @property
    def on_vercel(self) -> bool:
        import os

        return os.environ.get("VERCEL") == "1"

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    def redacted(self) -> dict[str, object]:
        """Config summary safe to log (no secrets)."""
        return {
            "environment": self.environment,
            "role": self.role,
            "storage_backend": self.storage_backend,
            "execution_backend": self.execution_backend,
            "embedding_backend": self.embedding_backend,
            "gemini_configured": bool(self.gemini_api_key),
            "run_embedded_worker": self.run_embedded_worker,
        }


class ConfigurationError(RuntimeError):
    """Raised when production settings are missing; the API then serves a 503 explanation."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            msg = str(err.get("msg", ""))
            msg = msg.removeprefix("Value error, ")
            problems.extend(p.strip() for p in msg.split(";") if p.strip())
        raise ConfigurationError(problems or ["invalid configuration"]) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
