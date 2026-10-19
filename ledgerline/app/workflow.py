"""Invoice status is an explicit state machine.

Every status change goes through transition(), which refuses moves that make
no sense (approving an invoice that was never reviewed, re-opening a rejected
one) and records each change in the audit log.

    uploaded ──> extracting ──> needs_review ──> approved
                     │               │
                     v               └─────────> rejected
             extraction_failed ──> extracting   (retry)
"""

from sqlalchemy.orm import Session

from app import audit
from app.models import Invoice

ALLOWED: dict[str, set[str]] = {
    "uploaded": {"extracting"},
    "extracting": {"needs_review", "extraction_failed"},
    "extraction_failed": {"extracting"},
    "needs_review": {"approved", "rejected", "extracting"},
    "approved": set(),  # terminal: payment happens downstream
    "rejected": set(),
}


class InvalidTransition(Exception):
    pass


def transition(session: Session, invoice: Invoice, new_status: str, actor_id: str | None, reason: str | None = None) -> None:
    old = invoice.status
    if new_status not in ALLOWED.get(old, set()):
        raise InvalidTransition(f"Cannot move invoice from {old!r} to {new_status!r}")
    invoice.status = new_status
    audit.record(
        session,
        tenant_id=invoice.tenant_id,
        actor_id=actor_id,
        entity="invoice",
        entity_id=invoice.id,
        action=f"status:{new_status}",
        before={"status": old},
        after={"status": new_status, **({"reason": reason} if reason else {})},
    )
