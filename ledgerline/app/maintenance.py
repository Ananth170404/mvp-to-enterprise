"""Housekeeping jobs.

recover_stuck_extractions() finds invoices whose extraction never finished:

  * "extracting" for longer than any extraction can take: every attempt died
    part-way, and the queue gave up (the job is in the dead-letter queue)
  * "uploaded" for much longer than the queue normally takes: the job was
    never queued (the app crashed between saving the invoice and sending the
    job), or it failed before starting, every time

It marks them failed, so they show up for a person to retry instead of
sitting there forever. The worker runs it every few minutes (app/worker.py),
and it can be run by hand:

    python -m app.maintenance recover-stuck            # mark them failed
    python -m app.maintenance recover-stuck --retry    # mark failed, then queue them again

The queue (Chapter 6) makes this rare, but the habit stays: anything that can
start must have a way to be noticed if it never ends.
"""

import argparse
import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select

from app import workflow
from app.config import settings
from app.db import SessionLocal, set_tenant
from app.models import Invoice, Tenant, utcnow

log = logging.getLogger("ledgerline.maintenance")


# A job can legitimately wait in the queue during a backlog, or while workers
# scale up from zero; only call it lost after much longer than that.
NEVER_STARTED_AFTER_MINUTES = 60


def recover_stuck_extractions(older_than_minutes: int | None = None, session_factory=SessionLocal) -> list[str]:
    return [invoice_id for _, invoice_id in _recover(older_than_minutes, session_factory)]


def _recover(older_than_minutes: int | None, session_factory) -> list[tuple[str, str]]:
    """Returns (tenant_id, invoice_id) for each invoice marked failed."""
    recovered: list[tuple[str, str]] = []
    session = session_factory()
    try:
        # The tenants table isn't tenant-scoped; everything else is (row-level
        # security, Chapter 7). A system job works through tenants one at a time,
        # with the same restricted access as a request, rather than with a
        # database user that can see everything.
        tenant_ids = session.scalars(select(Tenant.id)).all()
    finally:
        session.close()
    for tenant_id in tenant_ids:
        recovered += [(tenant_id, i) for i in _recover_tenant(tenant_id, older_than_minutes, session_factory)]
    if recovered:
        log.warning("Recovered %d stuck extraction(s): %s", len(recovered), ", ".join(i for _, i in recovered))
    return recovered


def _recover_tenant(tenant_id: str, older_than_minutes: int | None, session_factory) -> list[str]:
    minutes = older_than_minutes if older_than_minutes is not None else settings.stuck_after_minutes
    cutoff = utcnow() - timedelta(minutes=minutes)
    never_started = utcnow() - timedelta(minutes=max(minutes, NEVER_STARTED_AFTER_MINUTES))
    session = session_factory()
    set_tenant(session, tenant_id)
    recovered: list[str] = []
    try:
        # SKIP LOCKED: if two instances start at once, each row is handled by one of them.
        stuck = session.scalars(
            select(Invoice)
            .where(or_(
                and_(Invoice.status == "extracting", Invoice.status_changed_at < cutoff),
                and_(Invoice.status == "uploaded", Invoice.status_changed_at < never_started),
            ))
            .where(Invoice.tenant_id == tenant_id)
            .with_for_update(skip_locked=True)
        ).all()
        for invoice in stuck:
            reason = (f"Interrupted: still extracting after {minutes} minutes" if invoice.status == "extracting"
                      else "Never started: no worker picked up the extraction")
            workflow.transition(session, invoice, "extraction_failed", actor_id=None, reason=reason)
            recovered.append(invoice.id)
        session.commit()
    finally:
        session.close()
    return recovered


def unlink_identity(email: str) -> None:
    """The next sign-in with this verified email links the new identity.
    Use when a person's identity at the provider was recreated: a new
    provider, a deleted and re-created account. Ends their sessions too."""
    from app.models import User, UserSession

    session = SessionLocal()
    try:
        user = session.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            raise SystemExit(f"No user {email}")
        set_tenant(session, user.tenant_id)
        old = user.oidc_subject
        user.oidc_subject = None
        session.query(UserSession).filter(UserSession.user_id == user.id).delete()
        from app import audit

        audit.record(session, tenant_id=user.tenant_id, actor_id=None, entity="user", entity_id=user.id,
                     action="identity_unlinked", before={"oidc_subject": old})
        session.commit()
        print(f"Unlinked {email} (was {old}); their sessions are ended.")
    finally:
        session.close()


def main() -> None:
    from app.observability import configure_logging

    configure_logging(settings.log_format)
    parser = argparse.ArgumentParser(description="Ledgerline maintenance jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    stuck = sub.add_parser("recover-stuck", help="Fail (and optionally retry) extractions that never finished")
    stuck.add_argument("--minutes", type=int, default=None)
    stuck.add_argument("--retry", action="store_true")
    unlink = sub.add_parser("unlink-identity",
                            help="Forget which sign-in identity a user is linked to (e.g. after an identity-provider change)")
    unlink.add_argument("--email", required=True)
    args = parser.parse_args()

    if args.command == "unlink-identity":
        unlink_identity(args.email)
        return

    if args.command == "recover-stuck":
        recovered = _recover(args.minutes, SessionLocal)
        print(f"Recovered {len(recovered)} stuck extraction(s)")
        if args.retry:
            from app import jobs

            for tenant_id, invoice_id in recovered:
                session = SessionLocal()
                set_tenant(session, tenant_id)
                try:
                    jobs.enqueue_extraction(session.get(Invoice, invoice_id))
                finally:
                    session.close()
            print(f"Queued {len(recovered)} invoice(s) for extraction again")


if __name__ == "__main__":
    main()
