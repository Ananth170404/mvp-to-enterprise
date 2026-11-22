"""Who is making this request? (Chapter 7)

Sign-in is OpenID Connect (OIDC), the authorization code flow with PKCE:

    browser ── /auth/login ──> provider's sign-in page (Cognito in AWS, Keycloak locally)
       │                          │  password, MFA, "forgot password": all the provider's job
       │ <── /auth/callback?code&state ──┘
       │
    app: check state (this browser started this sign-in), swap the code for
         tokens directly with the provider, verify the ID token's signature,
         issuer, audience, expiry and nonce, then find the Ledgerline user
         (by the provider's permanent ID, or on first sign-in by verified email)
       │
    app: create a session; the browser gets an opaque random cookie

What lives where:
  * The provider knows *who someone is* (passwords, MFA, recovery).
  * Ledgerline's own users table decides *what they may do*: which tenant,
    which role. A person the provider knows but Ledgerline doesn't (nobody
    invited them) gets "not set up", never an account.
  * Sessions are rows in our database, so signing out, an admin disabling
    someone, or "sign out everywhere" take effect immediately. The cookie is
    a random token; the table stores only its hash.

The development login (DEV_LOGIN, a list of seeded users) still exists for
local work and tests. It creates the same kind of session, and the app refuses
to start with it in production (config.py).
"""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.db import get_session, set_tenant
from app.models import LoginRequest, User, UserSession, utcnow
from app.observability import set_user_context

log = logging.getLogger("ledgerline.auth")

COOKIE = "ll_session"
STATE_COOKIE = "ll_login_state"
LOGIN_REQUEST_TTL = timedelta(minutes=10)
TOUCH_EVERY = timedelta(minutes=5)  # how often last_seen_at is written (not on every request)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    tenant_id: str
    name: str
    role: str

    @property
    def can_approve(self) -> bool:
        return self.role == "approver"


class LoginRequired(Exception):
    pass


class SignInFailed(Exception):
    """Shown to the person as a sign-in error page; the reason is logged."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    from datetime import UTC

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the zone


# ---------------------------------------------------------------- the current user


def current_user(request: Request, session: Session = Depends(get_session)) -> CurrentUser:
    token = request.cookies.get(COOKIE)
    row = session.get(UserSession, _hash(token)) if token else None
    now = utcnow()
    if row is None:
        raise LoginRequired()
    idle_limit = timedelta(minutes=settings.session_idle_minutes)
    if _aware(row.expires_at) <= now or _aware(row.last_seen_at) + idle_limit <= now:
        session.delete(row)
        session.commit()
        raise LoginRequired()
    user = session.get(User, row.user_id)
    if user is None:
        raise LoginRequired()
    if now - _aware(row.last_seen_at) > TOUCH_EVERY:
        row.last_seen_at = now
        session.commit()
    # From here on, this request's database work is scoped to the user's tenant
    # (row-level security in Postgres), and every log line and error carries it.
    set_tenant(session, user.tenant_id)
    set_user_context(user.tenant_id, user.id)
    return CurrentUser(id=user.id, tenant_id=user.tenant_id, name=user.name, role=user.role)


def create_session(session: Session, user: User, method: str, user_agent: str | None,
                   id_token: str | None = None) -> str:
    """Start a session for this user; returns the token for the cookie."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(UserSession(
        id=_hash(token), user_id=user.id, created_at=now, last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_hours), method=method,
        user_agent=(user_agent or "")[:300] or None, id_token=id_token,
    ))
    user.last_login_at = now
    set_tenant(session, user.tenant_id)
    audit.record(session, tenant_id=user.tenant_id, actor_id=user.id, entity="user", entity_id=user.id,
                 action="signed_in", after={"method": method})
    session.commit()
    log.info("Signed in", extra={"event": "signed_in", "user_id": user.id, "tenant_id": user.tenant_id,
                                 "method": method})
    return token


def end_session(session: Session, token: str | None) -> UserSession | None:
    """Sign out: delete the session. Returns the deleted session (or None)."""
    row = session.get(UserSession, _hash(token)) if token else None
    if row is None:
        return None
    session.delete(row)
    session.commit()
    return row


def delete_expired(session: Session) -> int:
    """Housekeeping, run by the worker's sweep: expired sessions and abandoned sign-ins."""
    now = utcnow()
    n = session.execute(delete(UserSession).where(UserSession.expires_at < now)).rowcount
    session.execute(delete(LoginRequest).where(LoginRequest.created_at < now - LOGIN_REQUEST_TTL))
    session.commit()
    return n


# ---------------------------------------------------------------- OpenID Connect


class _Provider:
    """The provider's configuration (its discovery document) and signing keys, cached."""

    def __init__(self):
        self._config: dict | None = None
        self._fetched = 0.0
        self._jwks: jwt.PyJWKClient | None = None

    def config(self) -> dict:
        if self._config is None or time.monotonic() - self._fetched > 3600:
            response = httpx.get(settings.oidc_discovery_url, timeout=10)
            response.raise_for_status()
            self._config, self._fetched = response.json(), time.monotonic()
            self._jwks = jwt.PyJWKClient(self._config["jwks_uri"], cache_keys=True, lifespan=3600)
        return self._config

    def signing_key(self, id_token: str):
        self.config()
        return self._jwks.get_signing_key_from_jwt(id_token).key


provider = _Provider()


def oidc_enabled() -> bool:
    return bool(settings.oidc_discovery_url)


def redirect_uri(request: Request) -> str:
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/auth/callback"


def safe_return_to(value: str | None) -> str:
    # Only paths on this site: "/invoices/123", never "//evil.example" or "https://…".
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/invoices"


def begin_login(session: Session, request: Request, return_to: str | None) -> tuple[str, str]:
    """Returns (the provider URL to send the browser to, the state for the browser's cookie)."""
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # PKCE: proves the code is redeemed by whoever asked for it
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session.execute(delete(LoginRequest).where(LoginRequest.created_at < utcnow() - LOGIN_REQUEST_TTL))
    session.add(LoginRequest(state_hash=_hash(state), nonce=nonce, code_verifier=verifier,
                             return_to=safe_return_to(return_to)))
    session.commit()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri(request),
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{provider.config()['authorization_endpoint']}?{urlencode(params)}", state


def finish_login(session: Session, request: Request, code: str, state: str) -> tuple[User, str, str]:
    """The provider sent the browser back. Returns (the user, where to go next, the ID token)."""
    # 1. This browser started this sign-in (the state matches its cookie), and
    #    it's one we issued, recently, and haven't used yet.
    if not state or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE, "")):
        raise SignInFailed("This sign-in link has expired or was started in another browser. Please try again.")
    pending = session.get(LoginRequest, _hash(state))
    if pending is None or _aware(pending.created_at) < utcnow() - LOGIN_REQUEST_TTL:
        raise SignInFailed("This sign-in has expired. Please try again.")
    session.delete(pending)
    session.commit()

    # 2. Swap the code for tokens, server to server, with our client secret and the PKCE verifier.
    config = provider.config()
    response = httpx.post(
        config["token_endpoint"],
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(request),
              "code_verifier": pending.code_verifier, "client_id": settings.oidc_client_id},
        auth=(settings.oidc_client_id, settings.oidc_client_secret),
        timeout=10,
    )
    if response.status_code != 200:
        log.warning("Token exchange failed: %s %s", response.status_code, response.text[:300])
        raise SignInFailed("Sign-in couldn't be completed. Please try again.")
    id_token = response.json().get("id_token")

    # 3. Verify the ID token: signed by the provider's key, for us, from this
    #    issuer, not expired, and carrying the nonce we generated.
    try:
        claims = jwt.decode(
            id_token, provider.signing_key(id_token), algorithms=["RS256"],
            audience=settings.oidc_client_id, issuer=config["issuer"], leeway=60,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except (jwt.PyJWTError, TypeError) as exc:
        log.warning("ID token rejected: %s", exc)
        raise SignInFailed("Sign-in couldn't be verified. Please try again.") from exc
    if not secrets.compare_digest(claims.get("nonce", ""), pending.nonce):
        raise SignInFailed("Sign-in couldn't be verified. Please try again.")

    # 4. Which Ledgerline user is this? The provider's subject ID once linked;
    #    the first time, a verified email that matches someone we invited.
    subject = f"{claims['iss']}|{claims['sub']}"
    user = session.scalar(select(User).where(User.oidc_subject == subject))
    if user is None:
        email = (claims.get("email") or "").strip().lower()
        verified = claims.get("email_verified") in (True, "true")
        if not email or not verified:
            raise SignInFailed("Your sign-in has no verified email address, so we can't match it to an account.",
                               status=403)
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            log.info("Sign-in by someone with no account", extra={"event": "signin_no_account"})
            raise SignInFailed("You've signed in, but you don't have a Ledgerline account yet. "
                               "Ask an administrator at your company to invite you.", status=403)
        if user.oidc_subject:  # linked to a different identity already
            raise SignInFailed("This email is linked to a different sign-in. Contact your administrator.",
                               status=403)
        user.oidc_subject = subject
        log.info("Linked sign-in identity", extra={"event": "identity_linked", "user_id": user.id})
    return user, pending.return_to, id_token


def logout_url(request: Request, id_token: str | None) -> str | None:
    """Where to send the browser so the provider signs them out too (or None)."""
    if not oidc_enabled():
        return None
    endpoint = settings.oidc_logout_url or provider.config().get("end_session_endpoint")
    if not endpoint:
        return None
    back = (settings.public_base_url or str(request.base_url).rstrip("/")) + "/login"
    # Cognito reads logout_uri; standard OIDC providers (Keycloak) read
    # post_logout_redirect_uri and id_token_hint, which proves the request comes
    # from the app the person signed in to, so they sign out without a
    # confirmation page. Each ignores the parameters it doesn't use.
    params = {"client_id": settings.oidc_client_id, "logout_uri": back, "post_logout_redirect_uri": back}
    if id_token:
        params["id_token_hint"] = id_token
    return f"{endpoint}?{urlencode(params)}"
