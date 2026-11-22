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
    # Real sign-in (Chapter 7): OpenID Connect. Set the discovery URL to turn it
    # on; Cognito in AWS, Keycloak in local Docker. Both can be on in
    # development, so the seeded users stay one click away.
    oidc_discovery_url: str | None
    oidc_client_id: str | None
    oidc_client_secret: str | None
    # Where the provider sends people back. Must match what's registered with it.
    public_base_url: str | None
    # Providers that don't advertise a logout endpoint (Cognito) need it given.
    oidc_logout_url: str | None
    session_hours: int  # absolute: sign in again after this, whatever happens
    session_idle_minutes: int  # signed out after this long without a request
    site_password: str | None  # shared password in front of a non-production deploy
    max_upload_bytes: int
    fake_llm_manifest: Path
    stuck_after_minutes: int
    # Observability (Chapter 3). OpenTelemetry reads its own OTEL_* variables.
    sentry_dsn: str | None = None
    log_format: str = "json"  # json | text
    git_sha: str = "unknown"  # baked into the image at build time
    # Make the fake provider as slow as a real model, to see how background work
    # behaves on a platform (Chapter 4: Lambda freezes it) or under load (Chapter 6).
    fake_llm_delay_seconds: float = 0
    # Background jobs (Chapter 6). "sqs": the app puts a message on a queue and
    # app.worker does the work. "inline": the job runs immediately, in the same
    # process (tests, and running the app without Docker).
    queue_backend: str = "inline"
    job_queue_url: str | None = None
    # Only for local development: an SQS-compatible server (ElasticMQ).
    sqs_endpoint_url: str | None = None
    # How long a received job stays invisible to other workers. The worker
    # extends it while a job runs, so it only matters when a worker dies:
    # then the job is retried after at most this long.
    job_visibility_seconds: int = 120

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
        oidc_discovery_url=os.getenv("OIDC_DISCOVERY_URL") or None,
        oidc_client_id=os.getenv("OIDC_CLIENT_ID") or None,
        oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET") or None,
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or "").rstrip("/") or None,
        oidc_logout_url=os.getenv("OIDC_LOGOUT_URL") or None,
        session_hours=int(os.getenv("SESSION_HOURS", "12")),
        session_idle_minutes=int(os.getenv("SESSION_IDLE_MINUTES", "120")),
        site_password=os.getenv("SITE_PASSWORD") or None,
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024,
        fake_llm_manifest=Path(os.getenv("FAKE_LLM_MANIFEST", base / "data" / "generated" / "manifest.json")),
        # Longer than the slowest possible extraction (3 attempts x 120 s timeout + backoff).
        stuck_after_minutes=int(os.getenv("STUCK_AFTER_MINUTES", "10")),
        sentry_dsn=os.getenv("SENTRY_DSN") or None,
        log_format=os.getenv("LOG_FORMAT", "json"),
        git_sha=os.getenv("GIT_SHA", "unknown"),
        fake_llm_delay_seconds=float(os.getenv("FAKE_LLM_DELAY_SECONDS", "0")),
        queue_backend=os.getenv("QUEUE_BACKEND", "inline"),
        job_queue_url=os.getenv("JOB_QUEUE_URL") or None,
        sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL") or None,
        job_visibility_seconds=int(os.getenv("JOB_VISIBILITY_SECONDS", "120")),
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
    if s.oidc_discovery_url and not (s.oidc_client_id and s.oidc_client_secret):
        raise RuntimeError("OIDC_DISCOVERY_URL requires OIDC_CLIENT_ID and OIDC_CLIENT_SECRET.")
    if s.is_deployed and s.oidc_discovery_url and not (s.public_base_url or "").startswith("https://"):
        raise RuntimeError("Deployed sign-in requires PUBLIC_BASE_URL, starting with https://.")
    if s.is_production and not s.oidc_discovery_url:
        raise RuntimeError("Production requires real sign-in (OIDC_DISCOVERY_URL).")
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
    if s.queue_backend not in {"inline", "sqs"}:
        raise RuntimeError(f"QUEUE_BACKEND must be inline or sqs, got {s.queue_backend!r}.")
    # Inline jobs run inside the web request and die with the process (a deploy,
    # a crash). Deployed environments use the queue and a separate worker.
    if s.is_deployed and s.queue_backend != "sqs":
        raise RuntimeError("Deployed environments must use QUEUE_BACKEND=sqs.")
    if s.queue_backend == "sqs" and not s.job_queue_url:
        raise RuntimeError("QUEUE_BACKEND=sqs requires JOB_QUEUE_URL.")
    if s.log_format not in {"json", "text"}:
        raise RuntimeError(f"LOG_FORMAT must be json or text, got {s.log_format!r}.")


settings = load_settings()
