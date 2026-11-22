"""Web-app hardening (guide Chapter 8): response headers, the client's real
address, and rate limits.

Headers. Every response gets a strict Content-Security-Policy: no scripts at
all (Ledgerline has none), styles only from /static, no plugins, no framing
by other sites, forms only to ourselves and the sign-in provider. A bug that
lets someone inject HTML into a page then can't run code there. That's why
the templates have no <style> blocks or style="" attributes: inline CSS would
need 'unsafe-inline'.

Client address. Behind CloudFront and a load balancer, the TCP peer is the
load balancer. The real client is in X-Forwarded-For, but the client can put
anything in that header itself: each proxy only APPENDS what it saw. So:

    X-Forwarded-For: <anything the client sent>, <client as CloudFront saw it>, <CloudFront edge as the ALB saw it>
                                                  ^ the one to trust: TRUSTED_PROXY_HOPS = 2 from the right

uvicorn's --forwarded-allow-ips "*" (Ledgerline's setting until Chapter 8)
takes the LEFTMOST entry, which the client chose. Rate limits keyed on that
can be dodged with a header, and logs record a forged address.

Cross-site request forgery. A state-changing request (POST) must come from
our own pages: the browser's Sec-Fetch-Site / Origin headers must say so, and
a signed-in browser's form must carry its CSRF token, derived from the session
cookie that other sites can't read. SameSite=Lax cookies already stop most
cross-site posts; these close the rest (same-site subdomains, older browsers).

Rate limits. A fixed window per key ("sign-in from this address", "uploads by
this user"), counted in the database, so every app task shares the count.
"""

import hashlib
import hmac
import logging
import math
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RateLimit, utcnow

log = logging.getLogger("ledgerline.security")


# ---------------------------------------------------------------- headers


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


_form_targets: tuple[float, list[str]] = (0.0, [])


def _sign_in_origins() -> list[str]:
    """The sign-in provider's addresses, cached: this runs for every response, and
    must not wait on the provider each time (or at all, while it's down)."""
    global _form_targets
    expires, origins = _form_targets
    if time.monotonic() < expires:
        return origins
    origins, ttl = [], 300.0
    try:
        from app import auth

        if auth.oidc_enabled():
            config = auth.provider.config()
            for url in (config.get("authorization_endpoint"), config.get("end_session_endpoint"),
                        settings.oidc_logout_url):
                origin = _origin(url)
                if origin and origin not in origins:
                    origins.append(origin)
    except Exception:  # provider unreachable: a policy without it, retried in a minute
        log.warning("Could not read the sign-in provider's address for the CSP", exc_info=True)
        ttl = 60.0
    _form_targets = (time.monotonic() + ttl, origins)
    return origins


def content_security_policy() -> str:
    # Forms may submit to the sign-in provider: "Sign in" and "Sign out" post or
    # redirect there, and browsers apply form-action to redirects after a form too.
    form_targets = ["'self'", *_sign_in_origins()]
    return "; ".join([
        "default-src 'none'",
        "style-src 'self'",
        "img-src 'self'",
        "frame-src 'self'",          # the invoice PDF, shown in an iframe from our own origin
        f"form-action {' '.join(form_targets)}",
        "base-uri 'none'",
        "frame-ancestors 'none'",    # nobody may put Ledgerline in a frame (clickjacking)
    ])


def security_headers(request: Request) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if request.url.path.endswith("/document"):
        # The invoice itself, framed by our own invoice page. A PDF viewer needs
        # to run, so no default-src here; only who may frame it.
        headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        headers["Content-Security-Policy"] = content_security_policy()
        headers["X-Frame-Options"] = "DENY"
    if settings.is_deployed:
        # Browsers remember for a year: this site is HTTPS only.
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith("/static/"):
        # Pages show financial data: don't keep copies in shared or browser caches.
        headers["Cache-Control"] = "no-store"
    return headers


# ---------------------------------------------------------------- cross-site request forgery

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def csrf_token(request: Request) -> str:
    """The token for this browser's forms: a hash of its session cookie. Empty
    before sign-in (the sign-in forms rely on the origin check alone)."""
    from app.auth import COOKIE

    session_token = request.cookies.get(COOKIE)
    return hashlib.sha256(f"csrf|{session_token}".encode()).hexdigest() if session_token else ""


def _own_origin(request: Request) -> str:
    return settings.public_base_url or f"{request.url.scheme}://{request.url.netloc}"


async def csrf_protect(request: Request) -> None:
    """A dependency on every route; checks only unsafe methods."""
    if request.method not in UNSAFE_METHODS:
        return
    # Browsers say where a request came from; anything not from our own pages is refused.
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    origin = request.headers.get("origin")
    if origin and origin != "null" and origin != _own_origin(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    expected = csrf_token(request)
    if expected:  # signed in: the form must carry this browser's token
        supplied = (await request.form()).get("csrf_token", "")
        if not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="This form has expired. Go back, reload and try again.")


# ---------------------------------------------------------------- the client's address


def client_ip(request: Request) -> str:
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- rate limits

# (limit, window in seconds). Generous for people, tight for scripts.
LIMITS = {
    "signin": (30, 600),     # sign-in attempts per address per 10 minutes
    "upload": (120, 3600),   # uploads per user per hour
}


def rate_limit(session: Session, kind: str, subject: str) -> None:
    """Count one event; raise 429 once the window's limit is passed."""
    limit, window = LIMITS[kind]
    now = time.time()
    window_start = int(now // window) * window
    count = _increment(session, f"{kind}:{subject}", window_start)
    if count > limit:
        retry_after = max(1, math.ceil(window_start + window - now))
        log.warning("Rate limit hit", extra={"event": "rate_limited", "limit_kind": kind, "count": count})
        raise HTTPException(status_code=429, detail="Too many requests. Please wait and try again.",
                            headers={"Retry-After": str(retry_after)})


def _increment(session: Session, key: str, window_start: int) -> int:
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(RateLimit).values(key=key, window_start=window_start, count=1, updated_at=utcnow())
    statement = statement.on_conflict_do_update(
        index_elements=["key", "window_start"],
        set_={"count": RateLimit.count + 1, "updated_at": utcnow()},
    ).returning(RateLimit.count)
    count = session.execute(statement).scalar_one()
    session.commit()
    return count


def delete_old_windows(session: Session) -> None:
    """Housekeeping, run by the worker's sweep."""
    session.execute(delete(RateLimit).where(RateLimit.window_start < time.time() - 86400))
    session.commit()
