"""Test setup: SQLite, a temp storage folder, and the fake LLM provider.

Settings are read from the environment at import time, so the environment is
set before anything from `app` is imported.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ledgerline-tests-"))
GENERATED = TMP / "generated"

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{TMP / 'test.db'}",
        "STORAGE_DIR": str(TMP / "storage"),
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "gemini-3.5-flash-lite",
        "FAKE_LLM_MANIFEST": str(GENERATED / "manifest.json"),
        "DEV_LOGIN": "true",
    }
)

# Generate a fixed synthetic dataset once for the whole test run.
subprocess.run(
    [sys.executable, "-m", "scripts.generate_invoices", "--count", "30", "--seed", "7", "--out", str(GENERATED)],
    cwd=ROOT, check=True, capture_output=True,
)

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  (import routes once)
from app.models import Tenant, User  # noqa: E402
from app.purchase_orders import import_csv  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    acme = Tenant(name="Acme Manufacturing Pvt Ltd", tax_id="33AABCA1234D1ZK")
    globex = Tenant(name="Globex Retail LLP", tax_id="29AAGFG5678H1ZW")
    session.add_all([acme, globex])
    session.flush()
    users = {
        "clerk": User(tenant_id=acme.id, email="priya@acme.example", name="Priya", role="clerk"),
        "approver": User(tenant_id=acme.id, email="arjun@acme.example", name="Arjun", role="approver"),
        "other_tenant": User(tenant_id=globex.id, email="meera@globex.example", name="Meera", role="approver"),
    }
    session.add_all(users.values())
    session.flush()
    import_csv(session, acme.id, None, (GENERATED / "purchase_orders.csv").read_text())
    session.commit()
    yield session, users
    session.close()


@pytest.fixture()
def manifest() -> list[dict]:
    return json.loads((GENERATED / "manifest.json").read_text())["invoices"]
