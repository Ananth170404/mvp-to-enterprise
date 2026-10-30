"""Who is making this request?

v0 has a DEV-ONLY login: pick a seeded user and we set a cookie with their ID.
That is not authentication, and the app refuses to start with it enabled in
production (see config.py). Chapter 7 replaces this module with a real auth
provider. Every route already depends on current_user(), so only this file
changes.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import User
from app.observability import set_user_context

COOKIE = "ll_dev_user"


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


def current_user(request: Request, session: Session = Depends(get_session)) -> CurrentUser:
    if not settings.dev_login:
        raise HTTPException(status_code=501, detail="Authentication is not configured (see Chapter 7).")
    user_id = request.cookies.get(COOKIE)
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise LoginRequired()
    set_user_context(user.tenant_id, user.id)  # every later log line and error carries them
    return CurrentUser(id=user.id, tenant_id=user.tenant_id, name=user.name, role=user.role)
