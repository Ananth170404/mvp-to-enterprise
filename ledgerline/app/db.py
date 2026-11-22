from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------- tenant context (Chapter 7)
#
# In Postgres, row-level security (migration 0005) shows the app's database
# user only the rows whose tenant_id equals the setting app.tenant_id. So
# every transaction must say which tenant it's working for:
#
#     set_tenant(session, user.tenant_id)      # done for you by auth.current_user
#
# The setting is transaction-local (set_config(..., true)): it ends with the
# transaction, so a pooled connection never carries one request's tenant into
# the next. Without it, queries see no tenant rows at all: forgetting fails
# closed, not open. (SQLite, used by the tests, has no row-level security; the
# Postgres behaviour is checked by scripts/rls_check.py, in CI.)

TENANT_KEY = "tenant_id"
_SET = text("SELECT set_config('app.tenant_id', :tenant, true)")


def set_tenant(session: Session, tenant_id: str | None) -> None:
    """Scope this session's transactions to one tenant (or to none)."""
    session.info[TENANT_KEY] = tenant_id
    if session.in_transaction() and session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(_SET, {"tenant": tenant_id or ""})


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, transaction, connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(_SET, {"tenant": session.info.get(TENANT_KEY) or ""})


def tenant_scope(connection, tenant_id: str) -> None:
    """The same, for code using a Connection directly (scripts, the drill)."""
    if connection.dialect.name == "postgresql":
        connection.execute(_SET, {"tenant": tenant_id})
