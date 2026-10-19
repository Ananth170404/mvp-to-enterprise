"""All configuration comes from environment variables.

Nothing secret or environment-specific is hard-coded. The same container image
runs in development and production; only the environment differs.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    storage_dir: Path
    llm_provider: str
    llm_model: str
    gemini_api_key: str | None
    dev_login: bool
    max_upload_bytes: int
    fake_llm_manifest: Path

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings() -> Settings:
    base = Path(__file__).resolve().parent.parent
    settings = Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://ledgerline:ledgerline-dev@localhost:5432/ledgerline",
        ),
        storage_dir=Path(os.getenv("STORAGE_DIR", base / "data" / "storage")),
        llm_provider=os.getenv("LLM_PROVIDER", "fake"),
        llm_model=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        dev_login=_bool(os.getenv("DEV_LOGIN"), default=True),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024,
        fake_llm_manifest=Path(
            os.getenv("FAKE_LLM_MANIFEST", base / "data" / "generated" / "manifest.json")
        ),
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    # Fail fast on configurations that are dangerous in production.
    if s.is_production and s.dev_login:
        raise RuntimeError("DEV_LOGIN must be false when APP_ENV=production.")
    if s.is_production and s.llm_provider == "fake":
        raise RuntimeError("LLM_PROVIDER=fake is not allowed in production.")
    if s.llm_provider == "gemini" and not s.gemini_api_key:
        raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY.")


settings = load_settings()
