"""Housekeeping jobs.

recover_stuck_extractions() finds invoices left in "extracting" for longer than
any extraction can take. That happens when the process running the extraction
dies mid-way: a deploy, a crash, an out-of-memory kill. Without this, those
invoices sit in "extracting" forever and nobody notices.

It runs on app startup, and can be run on a schedule:

    python -m app.maintenance recover-stuck            # mark them failed
    python -m app.maintenance recover-stuck --retry    # mark failed, then re-extract

In Chapter 6 a proper job queue makes this mostly unnecessary, but the habit
stays: anything that can start must have a way to be noticed if it never ends.
"""

import argparse
import logging
from datetime import timedelta

from sqlalchemy import select

from app import workflow
from app.config import settings
from app.db import SessionLocal
from app.models import Invoice, utcnow

log = logging.getLogger("ledgerline.maintenance")


def recover_stuck_extractions(older_than_minutes: int | None = None, session_factory=SessionLocal) -> list[str]:
    minutes = older_than_minutes if older_than_minutes is not None else settings.stuck_after_minutes
    cutoff = utcnow() - timedelta(minutes=minutes)
    session = session_factory()
    recovered: list[str] = []
    try:
        # SKIP LOCKED: if two instances start at once, each row is handled by one of them.
        stuck = session.scalars(
            select(Invoice)
            .where(Invoice.status == "extracting", Invoice.status_changed_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for invoice in stuck:
            workflow.transition(
                session, invoice, "extraction_failed", actor_id=None,
                reason=f"Interrupted: still extracting after {minutes} minutes",
            )
            recovered.append(invoice.id)
        session.commit()
    finally:
        session.close()
    if recovered:
        log.warning("Recovered %d stuck extraction(s): %s", len(recovered), ", ".join(recovered))
    return recovered


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
            from app.extraction import extract_invoice

            for invoice_id in ids:
                extract_invoice(invoice_id)
            print(f"Re-extracted {len(ids)} invoice(s)")


if __name__ == "__main__":
    main()
