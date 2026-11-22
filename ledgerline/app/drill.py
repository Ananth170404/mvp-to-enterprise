"""Restore drill (guide Chapter 5): prove the backups work by restoring one.

A backup you have never restored is a hope, not a backup. The drill:

    python -m app.drill prepare    create a "Drill Co" tenant with 500 invoices (idempotent)
    python -m app.drill state      print the database clock and a fingerprint of Drill Co's invoices
    python -m app.drill accident   the mistake: a bulk update that forgot its WHERE id = ...
                                   and rejected every Drill Co invoice

Then restore the database to a moment before the accident (RDS point-in-time
restore creates a NEW instance), run `state` against the restored instance by
pointing DB_HOST at it, and compare fingerprints. Same fingerprint = restored.

It runs as the app's own database user (ledgerline_app), the same rights a buggy
code path would have, and touches only the Drill Co tenant. It refuses to run
in production: a drill there is done on a restored copy, never on the original.

In ECS, run it as a one-off task (guide 5B): the database is reachable only
from inside the VPC.
"""

import sys

from sqlalchemy import create_engine, text

from app.config import settings
from app.db import tenant_scope

DRILL_TENANT = "Drill Co (restore drill)"
DRILL_USER = "drill@drill.example"
INVOICES = 500


def main() -> None:
    commands = {"prepare": prepare, "state": state, "accident": accident}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: python -m app.drill {{{'|'.join(commands)}}}")
    if settings.is_production:
        sys.exit("Refusing to run the restore drill against production. Drill on a restored copy.")
    engine = create_engine(settings.database_url)
    host = engine.url.host
    print(f"database host: {host}")
    commands[sys.argv[1]](engine)


def _tenant_id(conn) -> str | None:
    tenant_id = conn.scalar(text("SELECT id FROM tenants WHERE name = :n"), {"n": DRILL_TENANT})
    if tenant_id:
        tenant_scope(conn, tenant_id)  # row-level security: work as Drill Co (Chapter 7)
    return tenant_id


def prepare(engine) -> None:
    with engine.begin() as conn:
        tenant_id = _tenant_id(conn)
        if tenant_id is None:
            tenant_id = conn.scalar(
                text("INSERT INTO tenants (id, name, created_at) VALUES (gen_random_uuid()::text, :n, now()) RETURNING id"),
                {"n": DRILL_TENANT},
            )
            conn.execute(
                text("""INSERT INTO users (id, tenant_id, email, name, role)
                        VALUES (gen_random_uuid()::text, :t, :e, 'Drill user', 'approver')"""),
                {"t": tenant_id, "e": DRILL_USER},
            )
            tenant_scope(conn, tenant_id)
        existing = conn.scalar(text("SELECT count(*) FROM invoices WHERE tenant_id = :t"), {"t": tenant_id})
        if existing:
            print(f"Drill Co already has {existing} invoices; nothing to do.")
            return
        user_id = conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": DRILL_USER})
        params = {"t": tenant_id, "u": user_id, "n": INVOICES}
        conn.execute(
            text("""
            CREATE TEMP TABLE gen ON COMMIT DROP AS
            SELECT gen_random_uuid()::text AS invoice_id, gen_random_uuid()::text AS document_id,
                   md5('drill' || g::text) || md5(g::text) AS sha256, g,
                   CASE WHEN g % 10 < 7 THEN 'approved' ELSE 'needs_review' END AS status,
                   (1000 + g * 37 % 50000)::bigint * 100 AS total_minor
            FROM generate_series(1, :n) AS g
        """),
            params,
        )
        conn.execute(
            text("""
            INSERT INTO documents (id, tenant_id, storage_key, sha256, mime_type, original_filename,
                                   size_bytes, uploaded_by, created_at)
            SELECT document_id, :t, 'drill/' || sha256 || '.pdf', sha256, 'application/pdf',
                   'drill-' || g || '.pdf', 50000, :u, now()
            FROM gen
        """),
            params,
        )
        conn.execute(
            text("""
            INSERT INTO invoices (id, tenant_id, document_id, status, status_changed_at, vendor_name,
                                  vendor_tax_id, invoice_number, invoice_date, currency, total_minor,
                                  flags, created_at, updated_at)
            SELECT invoice_id, :t, document_id, status, now(), 'Drill Vendor ' || (g % 20),
                   'DRILL' || lpad((g % 20)::text, 10, '0'), 'DRILL-' || g, current_date, 'INR',
                   total_minor, '[]', now(), now()
            FROM gen
        """),
            params,
        )
    print(f"Created Drill Co with {INVOICES} invoices.")
    state(engine)


def state(engine) -> None:
    with engine.connect() as conn:
        tenant_id = _tenant_id(conn)
        if tenant_id is None:
            sys.exit("No Drill Co tenant here. Run `prepare` first.")
        # Rounded UP to the next whole second. Point-in-time restore takes whole
        # seconds; restoring to this second rounded DOWN can land a fraction of a
        # second before the rows you want (we did exactly that, guide 5B).
        safe = conn.scalar(
            text("""SELECT to_char(date_trunc('second', now() AT TIME ZONE 'UTC') + interval '1 second',
                                   'YYYY-MM-DD"T"HH24:MI:SS"Z"')""")
        )
        counts = conn.execute(
            text("SELECT status, count(*) FROM invoices WHERE tenant_id = :t GROUP BY status ORDER BY status"),
            {"t": tenant_id},
        ).all()
        # One hash over every Drill Co invoice's id, status and amount: if any row
        # differs between two databases, the fingerprints differ.
        fingerprint = conn.scalar(
            text("""SELECT md5(string_agg(id || ':' || status || ':' || coalesce(total_minor, 0), ',' ORDER BY id))
                    FROM invoices WHERE tenant_id = :t"""),
            {"t": tenant_id},
        )
    print(f"safe restore point (UTC): {safe}  (restore to this or later, but before any accident)")
    for status, count in counts:
        print(f"  {status:<14}{count:>5}")
    print(f"fingerprint: {fingerprint}")


def accident(engine) -> None:
    with engine.begin() as conn:
        tenant_id = _tenant_id(conn)
        if tenant_id is None:
            sys.exit("No Drill Co tenant here. Run `prepare` first.")
        # Meant to reject ONE invoice. The `AND id = :invoice_id` got lost in a refactor.
        bulk_reject = """UPDATE invoices SET status = 'rejected', status_changed_at = now(), updated_at = now()
                         WHERE tenant_id = :t"""
        result = conn.execute(text(bulk_reject), {"t": tenant_id})
        now = conn.scalar(text("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"))
    print(f"ACCIDENT at {now} (UTC): {result.rowcount} invoices set to 'rejected'.")


if __name__ == "__main__":
    main()
