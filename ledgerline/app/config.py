"""All configuration comes from environment variables.

Nothing secret or environment-specific is hard-coded. The same container image
runs in development, staging and production; only the environment differs.
On AWS, ECS injects secrets from Parameter Store and Secrets Manager into
these variables, so this file doesn't change between a laptop and the cloud.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ENVIRONMENTS = {"development", "test", "staging", "production"}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def database_url_from_env(prefix: str = "") -> str | None:
    """The database URL for one database user, from the environment.

    Either a full URL ({prefix}DATABASE_URL), or built from parts, which is how
    cloud platforms usually inject secrets: one value per variable.

        prefix ""           app user:     DB_USER, DB_PASSWORD
        prefix "MIGRATION_" schema owner: MIGRATION_DB_USER, MIGRATION_DB_PASSWORD
        shared by both:     DB_HOST, DB_PORT (5432), DB_NAME (ledgerline), DB_SSLMODE (require)

    Generated passwords contain characters like @ : / % that would corrupt a
    URL, so user and password are percent-encoded.
    """
    full = os.getenv(f"{prefix}DATABASE_URL")
    if full:
        return full
    host = os.getenv("DB_HOST")
    user = os.getenv(f"{prefix}DB_USER")
    password = os.getenv(f"{prefix}DB_PASSWORD")
    if not (host and user and password):
        return None
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "ledgerline")
    sslmode = os.getenv("DB_SSLMODE", "require")
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(name, safe='')}?sslmode={sslmode}"
    )


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    storage_backend: str  # local | s3
    storage_dir: Path
    s3_bucket: str | None
    aws_region: str | None
    llm_provider: str
    llm_model: str
    gemini_api_key: str | None
    dev_login: bool
    site_password: str | None  # shared password in front of a non-production deploy
    max_upload_bytes: int
    fake_llm_manifest: Path
    stuck_after_minutes: int
    # Observability (Chapter 3). OpenTelemetry reads its own OTEL_* variables.
    sentry_dsn: str | None = None
    log_format: str = "json"  # json | text
    git_sha: str = "unknown"  # baked into the image at build time

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_deployed(self) -> bool:
        """Running somewhere other than a developer's machine or a test run."""
        return self.app_env in {"staging", "production"}


def load_settings() -> Settings:
    base = Path(__file__).resolve().parent.parent
    settings = Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_url=database_url_from_env()
        or "postgresql+psycopg://ledgerline:ledgerline-dev@localhost:5432/ledgerline",
        storage_backend=os.getenv("STORAGE_BACKEND", "local"),
        storage_dir=Path(os.getenv("STORAGE_DIR", base / "data" / "storage")),
        s3_bucket=os.getenv("S3_BUCKET") or None,
        aws_region=os.getenv("AWS_REGION") or None,
        llm_provider=os.getenv("LLM_PROVIDER", "fake"),
        llm_model=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        dev_login=_bool(os.getenv("DEV_LOGIN"), default=True),
        site_password=os.getenv("SITE_PASSWORD") or None,
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024,
        fake_llm_manifest=Path(os.getenv("FAKE_LLM_MANIFEST", base / "data" / "generated" / "manifest.json")),
        # Longer than the slowest possible extraction (3 attempts x 120 s timeout + backoff).
        stuck_after_minutes=int(os.getenv("STUCK_AFTER_MINUTES", "10")),
        sentry_dsn=os.getenv("SENTRY_DSN") or None,
        log_format=os.getenv("LOG_FORMAT", "json"),
        git_sha=os.getenv("GIT_SHA", "unknown"),
    )
    validate(settings)
    return settings


def validate(s: Settings) -> None:
    """Fail fast on configurations that are dangerous where the app is running."""
    if s.app_env not in ENVIRONMENTS:
        raise RuntimeError(f"APP_ENV must be one of {sorted(ENVIRONMENTS)}, got {s.app_env!r}.")
    if s.is_production and s.dev_login:
        raise RuntimeError("DEV_LOGIN must be false when APP_ENV=production.")
    if s.is_production and s.llm_provider == "fake":
        raise RuntimeError("LLM_PROVIDER=fake is not allowed in production.")
    # A staging URL on the internet is reachable by anyone. With dev login on,
    # anyone could act as any user, so a deployed dev login needs a gate.
    if s.is_deployed and s.dev_login and not s.site_password:
        raise RuntimeError("DEV_LOGIN on a deployed environment requires SITE_PASSWORD.")
    # Container disks are thrown away on every deploy; files must live in S3.
    if s.is_deployed and s.storage_backend != "s3":
        raise RuntimeError("Deployed environments must use STORAGE_BACKEND=s3.")
    if s.storage_backend not in {"local", "s3"}:
        raise RuntimeError(f"Unknown STORAGE_BACKEND {s.storage_backend!r}.")
    if s.storage_backend == "s3" and not s.s3_bucket:
        raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET.")
    if s.llm_provider == "gemini" and not s.gemini_api_key:
        raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY.")
    if s.log_format not in {"json", "text"}:
        raise RuntimeError(f"LOG_FORMAT must be json or text, got {s.log_format!r}.")


settings = load_settings()
