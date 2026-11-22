"""Helpers shared by tests. Paths come from the environment set in conftest.py."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import COOKIE, create_session
from app.db import SessionLocal
from app.main import app
from app.models import User
from app.security import csrf_token

GENERATED = Path(os.environ["FAKE_LLM_MANIFEST"]).parent


class SignedInClient(TestClient):
    """Adds the form's CSRF token to every POST, as our own pages would (Chapter 8)."""

    def post(self, url, *args, data=None, **kwargs):
        data = dict(data or {})
        data.setdefault("csrf_token", csrf_for(self))
        return super().post(url, *args, data=data, **kwargs)


def csrf_for(client: TestClient) -> str:
    from starlette.requests import Request

    cookie = client.cookies.get(COOKIE)
    return csrf_token(Request({"type": "http", "headers": [(b"cookie", f"{COOKIE}={cookie}".encode())]}))


def client_for(user: User) -> TestClient:
    """A test client signed in as this user (a real session, as the dev login creates)."""
    session = SessionLocal()
    try:
        token = create_session(session, session.get(User, user.id), "dev", "pytest")
    finally:
        session.close()
    client = SignedInClient(app)
    client.cookies.set(COOKIE, token)
    return client


def upload(client: TestClient, item: dict):
    path = GENERATED / item["file"]
    return client.post("/invoices", files={"file": (path.name, path.read_bytes())}, follow_redirects=False)


def first(manifest: list[dict], scenario: str) -> dict:
    return next(i for i in manifest if i["scenario"] == scenario)
