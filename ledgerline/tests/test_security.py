"""Chapter 8: response headers, the client's address, rate limits."""

import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import security
from app.main import app
from tests.helpers import client_for, csrf_for, first, upload

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"


def test_every_page_gets_a_strict_content_security_policy(db):
    _, users = db
    for client, path in ((TestClient(app), "/login"), (client_for(users["clerk"]), "/invoices"),
                         (TestClient(app), "/no-such-page")):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp, path
        assert "unsafe-inline" not in csp and "script-src" not in csp  # no scripts, at all
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"


def test_templates_have_no_inline_styles_or_scripts():
    # The CSP above would block them; keep it that way.
    for template in TEMPLATES.glob("*.html"):
        html = template.read_text()
        assert not re.search(r"<script|<style|\sstyle=|\son[a-z]+=", html), template.name


def test_the_stylesheet_is_served_without_signing_in():
    r = TestClient(app).get("/static/app.css")
    assert r.status_code == 200 and "--ink" in r.text and "cache-control" not in r.headers


def test_the_sign_in_provider_is_an_allowed_form_target(monkeypatch):
    monkeypatch.setattr(security, "_form_targets", (0.0, []))
    monkeypatch.setattr("app.auth.oidc_enabled", lambda: True)
    monkeypatch.setattr("app.auth.provider.config", lambda: {
        "authorization_endpoint": "https://idp.example/oauth2/authorize",
        "end_session_endpoint": "https://idp.example/logout"})
    assert "form-action 'self' https://idp.example;" in security.content_security_policy()


def test_the_invoice_document_may_only_be_framed_by_ledgerline(db, manifest):
    _, users = db
    client = client_for(users["clerk"])
    invoice_url = upload(client, first(manifest, "clean")).headers["location"].split("?")[0]
    r = client.get(f"{invoice_url}/document")
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and r.headers["x-content-type-options"] == "nosniff"


def _request(xff: str | None, peer="10.0.0.5") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


@pytest.mark.parametrize("xff, hops, expected", [
    (None, 0, "10.0.0.5"),                                     # no proxies: the TCP peer
    ("203.0.113.9, 130.176.1.1", 2, "203.0.113.9"),            # client, CloudFront edge
    ("6.6.6.6, 203.0.113.9, 130.176.1.1", 2, "203.0.113.9"),   # a forged entry is ignored
    ("6.6.6.6", 2, "10.0.0.5"),                                 # too few hops: don't guess
])
def test_the_client_address_counts_only_our_own_proxies(monkeypatch, xff, hops, expected):
    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, trusted_proxy_hops=hops))
    assert security.client_ip(_request(xff)) == expected


def test_sign_in_attempts_are_rate_limited(db, monkeypatch):
    monkeypatch.setitem(security.LIMITS, "signin", (3, 600))
    client = TestClient(app)
    for _ in range(3):
        assert client.post("/login", data={"user_id": "nobody"}).status_code == 400
    r = client.post("/login", data={"user_id": "nobody"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_uploads_are_rate_limited_per_user(db, manifest, monkeypatch):
    _, users = db
    monkeypatch.setitem(security.LIMITS, "upload", (1, 3600))
    clerk, approver = client_for(users["clerk"]), client_for(users["approver"])
    assert upload(clerk, first(manifest, "clean")).status_code == 303
    assert upload(clerk, first(manifest, "clean")).status_code == 429
    assert upload(approver, first(manifest, "clean")).status_code == 303  # someone else's limit


def test_signed_in_forms_need_their_csrf_token(db):
    _, users = db
    client = client_for(users["clerk"])
    plain = TestClient(app, cookies=client.cookies)  # same session, but posts without the token
    r = plain.post("/logout", follow_redirects=False)
    assert r.status_code == 403
    r = plain.post("/logout", data={"csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403
    assert plain.post("/logout", data={"csrf_token": csrf_for(client)}, follow_redirects=False).status_code == 303


def test_posts_from_other_sites_are_refused(db):
    _, users = db
    client = client_for(users["clerk"])
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "https://evil.example"}):
        assert client.post("/logout", headers=headers, follow_redirects=False).status_code == 403, headers
    assert client.post("/logout", headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
                       follow_redirects=False).status_code == 303
