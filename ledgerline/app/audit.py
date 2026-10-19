from sqlalchemy.orm import Session

from app.models import AuditEvent


def record(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str | None,
    entity: str,
    entity_id: str,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """Append an audit event. There is deliberately no update or delete function."""
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            before=before,
            after=after,
        )
    )
