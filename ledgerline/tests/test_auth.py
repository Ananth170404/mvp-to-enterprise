"""Chapter 7: sign-in, against a fake OpenID Connect provider with a real RSA key.

The provider's two network calls (the token exchange, the signing keys) are
replaced; everything Ledgerline itself checks is real: state, nonce, signature,
issuer, audience, expiry, verified email, the invited-users rule, sessions.
"""

import dataclasses
import html
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.models import AuditEvent, UserSession, utcnow
from tests.helpers import client_for

ISSUER = "https://idp.example/realms/test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def idp(monkeypatch):
    """A fake provider. Set idp.claims / idp.key to shape the next ID token."""

    class FakeIdP:
        claims: dict = {}
        key = KEY
        nonce: str | None = None

    fake = FakeIdP()
    config = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token",
              "jwks_uri": f"{ISSUER}/certs", "end_session_endpoint": f"{ISSUER}/logout"}
    monkeypatch.setattr(auth, "settings", dataclasses.replace(
        auth.settings, oidc_discovery_url=f"{ISSUER}/.well-known/openid-configuration", oidc_client_id="ledgerline",
        oidc_client_secret="test-secret", public_base_url="http://testserver"))
    monkeypatch.setattr(auth.provider, "config", lambda: config)
    monkeypatch.setattr(auth.provider, "signing_key", lambda token: KEY.public_key())

    class TokenResponse:
        status_code = 200

        def json(self):
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "ledgerline", "sub": "sub-123", "iat": now, "exp": now + 300,
                      "nonce": fake.nonce, "email": "priya@acme.example", "email_verified": True, **fake.claims}
            return {"id_token": jwt.encode(claims, fake.key, algorithm="RS256")}

    def token_endpoint(url, data, auth, timeout):
        assert url == config["token_endpoint"] and data["code"] == "the-code" and data["code_verifier"]
        return TokenResponse()

    monkeypatch.setattr(auth.httpx, "post", token_endpoint)
    return fake


def _start(client: TestClient, idp, next_path="/invoices"):
    """Begin sign-in; returns the state the provider would send back."""
    r = client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 303
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == ["http://testserver/auth/callback"]
    idp.nonce = query["nonce"][0]
    return query["state"][0]


def test_sign_in_links_the_identity_and_starts_a_session(db, idp):
    session, users = db
    client = TestClient(app)
    state = _start(client, idp, "/invoices?status=needs_review")
    r = client.get("/auth/callback", params={"code": "the-code", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/invoices?status=needs_review"
    assert client.get("/invoices").status_code == 200
    session.expire_all()
    assert users["clerk"].oidc_subject == f"{ISSUER}|sub-123"
    assert session.query(AuditEvent).filter_by(entity="user", action="signed_in").count() == 1
    # The cookie is a random token; the database holds only its hash.
    token = client.cookies.get(auth.COOKIE)
    assert session.get(UserSession, token) is None and session.get(UserSession, auth._hash(token)) is not None


def test_a_linked_identity_is_found_by_subject_even_if_the_email_changes(db, idp):
    session, users = db
    users["clerk"].oidc_subject = f"{ISSUER}|sub-123"
    session.commit()
    idp.claims = {"email": "priya.new@acme.example"}
    client = TestClient(app)
    state = _start(client, idp)
    assert client.get("/auth/callback", params={"code": "the-code", "state": state},
                      follow_redirects=False).status_code == 303


@pytest.mark.parametrize("claims, key, message", [
    ({"email": "stranger@example.com"}, KEY, "don't have a Ledgerline account"),
    ({"email_verified": False}, KEY, "no verified email"),
    ({}, OTHER_KEY, "couldn't be verified"),             # signed by someone else
    ({"aud": "another-app"}, KEY, "couldn't be verified"),  # a token meant for a different app
    ({"iss": "https://evil.example"}, KEY, "couldn't be verified"),
    ({"exp": 1}, KEY, "couldn't be verified"),           # long expired
    ({"nonce": "replayed"}, KEY, "couldn't be verified"),
])
def test_bad_sign_ins_are_refused_without_a_session(db, idp, claims, key, message):
    idp.claims, idp.key = claims, key
    client = TestClient(app)
    state = _start(client, idp)
    r = client.get("/auth/callback", params={"code": "the-code", "state": state}, follow_redirects=False)
    assert r.status_code in (400, 403) and message in html.unescape(r.text)
    assert auth.COOKIE not in client.cookies


def test_a_callback_from_another_browser_or_replayed_is_refused(db, idp):
    starter, attacker = TestClient(app), TestClient(app)
    state = _start(starter, idp)
    # Login CSRF: someone else's browser presents a valid code and state.
    assert attacker.get("/auth/callback", params={"code": "the-code", "state": state}).status_code == 400
    assert starter.get("/auth/callback", params={"code": "the-code", "state": state},
                       follow_redirects=False).status_code == 303
    # The same state can't be used twice.
    assert starter.get("/auth/callback", params={"code": "the-code", "state": state}).status_code == 400


def test_sign_out_ends_the_session_here_and_at_the_provider(db, idp):
    client = TestClient(app)
    state = _start(client, idp)
    client.get("/auth/callback", params={"code": "the-code", "state": state}, follow_redirects=False)
    r = client.post("/logout", follow_redirects=False)
    target = urlsplit(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/logout"
    assert "id_token_hint" in parse_qs(target.query)
    client.cookies.clear()
    assert db[0].query(UserSession).count() == 0


def test_sessions_expire_when_idle_and_absolutely(db):
    session, users = db
    client = client_for(users["clerk"])
    assert client.get("/invoices", follow_redirects=False).status_code == 200
    row = session.query(UserSession).one()
    row.last_seen_at = utcnow() - timedelta(hours=3)  # idle limit: 2 hours
    session.commit()
    assert client.get("/invoices", follow_redirects=False).headers["location"].startswith("/login")

    client = client_for(users["clerk"])
    row = session.query(UserSession).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    assert client.get("/invoices", follow_redirects=False).headers["location"].startswith("/login")


def test_only_local_paths_are_accepted_as_where_to_go_next():
    assert auth.safe_return_to("/invoices/1") == "/invoices/1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "", None):
        assert auth.safe_return_to(bad) == "/invoices"
