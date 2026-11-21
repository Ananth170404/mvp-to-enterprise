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
from app.db import SessionLocal
from app.models import Invoice, utcnow

log = logging.getLogger("ledgerline.maintenance")


# A job can legitimately wait in the queue during a backlog, or while workers
# scale up from zero; only call it lost after much longer than that.
NEVER_STARTED_AFTER_MINUTES = 60


def recover_stuck_extractions(older_than_minutes: int | None = None, session_factory=SessionLocal) -> list[str]:
    minutes = older_than_minutes if older_than_minutes is not None else settings.stuck_after_minutes
    cutoff = utcnow() - timedelta(minutes=minutes)
    never_started = utcnow() - timedelta(minutes=max(minutes, NEVER_STARTED_AFTER_MINUTES))
    session = session_factory()
    recovered: list[str] = []
    try:
        # SKIP LOCKED: if two instances start at once, each row is handled by one of them.
        stuck = session.scalars(
            select(Invoice)
            .where(or_(
                and_(Invoice.status == "extracting", Invoice.status_changed_at < cutoff),
                and_(Invoice.status == "uploaded", Invoice.status_changed_at < never_started),
            ))
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
    if recovered:
        log.warning("Recovered %d stuck extraction(s): %s", len(recovered), ", ".join(recovered))
    return recovered


def main() -> None:
    from app.observability import configure_logging

    configure_logging(settings.log_format)
    parser = argparse.ArgumentParser(description="Ledgerline maintenance jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    stuck = sub.add_parser("recover-stuck", help="Fail (and optionally retry) extractions that never finished")
    stuck.add_argument("--minutes", type=int, default=None)
    stuck.add_argument("--retry", action="store_true")
    args = parser.parse_args()

    if args.command == "recover-stuck":
        ids = recover_stuck_extractions(args.minutes)
        print(f"Recovered {len(ids)} stuck extraction(s)")
        if args.retry:
            from app import jobs

            session = SessionLocal()
            try:
                for invoice_id in ids:
                    jobs.enqueue_extraction(session.get(Invoice, invoice_id))
            finally:
                session.close()
            print(f"Queued {len(ids)} invoice(s) for extraction again")


if __name__ == "__main__":
    main()
