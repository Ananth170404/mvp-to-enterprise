"""Helpers shared by tests. Paths come from the environment set in conftest.py."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import COOKIE
from app.main import app
from app.models import User

GENERATED = Path(os.environ["FAKE_LLM_MANIFEST"]).parent


def client_for(user: User) -> TestClient:
    client = TestClient(app)
    client.cookies.set(COOKIE, user.id)
    return client


def upload(client: TestClient, item: dict):
    path = GENERATED / item["file"]
    return client.post("/invoices", files={"file": (path.name, path.read_bytes())}, follow_redirects=False)


def first(manifest: list[dict], scenario: str) -> dict:
    return next(i for i in manifest if i["scenario"] == scenario)
