"""Fill a development database with a realistic volume of invoices (guide Chapter 5).

Queries that take 2 ms on 30 invoices can take seconds on a million. This
loads that million (or any number) so you can find out before customers do:

    docker compose run --rm api python -m scripts.load_volume --invoices 1000000

Realistic shape, because the shape decides which queries hurt:
  * skewed tenants: one large customer owns most of the rows (B2B data
    almost always looks like this), the rest are small
  * statuses like a working system: mostly approved, some in review
  * two years of history, a few hundred vendors per tenant
  * one extraction run per invoice (the spend query reads these)

Rows are generated inside Postgres with generate_series(): one INSERT ...
SELECT per table, about a minute for a million invoices on a laptop. Inserting
them one by one through the ORM would take hours.

Refuses to run anywhere but development, and only against Postgres.
"""

import argparse
import sys
import time

from sqlalchemy import create_engine, text

from app.config import database_url_from_env, settings

# Share of invoices per tenant, largest first. Applied to the tenants that exist
# (created by `python -m app.seed`), in alphabetical order of name.
TENANT_SHARE = [0.70, 0.25, 0.05]

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--invoices", type=int, default=1_000_000)
    args = parser.parse_args()

    if settings.app_env != "development":
        sys.exit(f"Refusing to load synthetic volume into APP_ENV={settings.app_env}.")
    url = database_url_from_env("MIGRATION_") or settings.database_url  # bulk load as the schema owner
    if not url.startswith("postgresql"):
        sys.exit("This script is for Postgres (it uses generate_series).")

    engine = create_engine(url)
    with engine.begin() as conn:
        tenants = conn.execute(text("SELECT id, name FROM tenants ORDER BY name")).all()
        if not tenants:
            sys.exit("No tenants: run `python -m app.seed` first.")
        users = dict(conn.execute(text("SELECT DISTINCT ON (tenant_id) tenant_id, id FROM users ORDER BY tenant_id")).all())

    started = time.monotonic()
    for (tenant_id, name), share in zip(tenants, TENANT_SHARE, strict=False):
        count = int(args.invoices * share)
        if not count or tenant_id not in users:
            continue
        t0 = time.monotonic()
        load_tenant(engine, tenant_id, users[tenant_id], count)
        print(f"{name}: {count:,} invoices in {time.monotonic() - t0:.0f} s")

    with engine.begin() as conn:
        # Fresh statistics, as autovacuum would eventually produce: without them the
        # planner guesses, and the "before" plans in the demo would be misleading.
        conn.execute(text("ANALYZE documents, invoices, extraction_runs"))
    print(f"Done in {time.monotonic() - started:.0f} s")


def load_tenant(engine, tenant_id: str, user_id: str, count: int) -> None:
    params = {"tenant": tenant_id, "user": user_id, "n": count}
    with engine.begin() as conn:
        # A temporary table of generated rows; each real table is filled from it.
        conn.execute(text("""
            CREATE TEMP TABLE gen ON COMMIT DROP AS
            SELECT
              gen_random_uuid()::text AS invoice_id,
              gen_random_uuid()::text AS document_id,
              gen_random_uuid()::text AS run_id,
              md5(random()::text || g::text) || md5(g::text) AS sha256,
              now() - (random() * interval '730 days') AS created_at,
              (random() * 400)::int AS vendor_no,
              g,
              -- statuses like a working system: mostly approved, some in review
              CASE
                WHEN r < 0.70 THEN 'approved'
                WHEN r < 0.85 THEN 'needs_review'
                WHEN r < 0.93 THEN 'rejected'
                WHEN r < 0.98 THEN 'extraction_failed'
                ELSE 'uploaded'
              END AS status,
              (1000 + random() * 500000)::bigint * 100 AS total_minor
            FROM (SELECT g, random() AS r FROM generate_series(1, :n) AS g) s
        """), params)
        conn.execute(text("""
            INSERT INTO documents (id, tenant_id, storage_key, sha256, mime_type, original_filename,
                                   size_bytes, uploaded_by, created_at)
            SELECT document_id, :tenant, 'synthetic/' || sha256 || '.pdf', sha256, 'application/pdf',
                   'invoice-' || g || '.pdf', 50000, :user, created_at
            FROM gen
        """), params)
        conn.execute(text("""
            INSERT INTO invoices (id, tenant_id, document_id, status, status_changed_at, vendor_name,
                                  vendor_tax_id, invoice_number, invoice_date, currency, subtotal_minor,
                                  tax_minor, total_minor, flags, created_at, updated_at)
            SELECT invoice_id, :tenant, document_id, status, created_at,
                   'Vendor ' || vendor_no, 'SYN' || lpad(vendor_no::text, 12, '0'),
                   'INV-' || g, created_at::date, 'INR',
                   (total_minor / 1.18)::bigint, total_minor - (total_minor / 1.18)::bigint,
                   total_minor, '[]', created_at, created_at
            FROM gen
        """), params)
        conn.execute(text("""
            INSERT INTO extraction_runs (id, tenant_id, invoice_id, provider, model, prompt_version,
                                         input_tokens, output_tokens, cost_usd, latency_ms, status, created_at)
            SELECT run_id, :tenant, invoice_id, 'fake', 'gemini-3.5-flash-lite', 'extract-v1',
                   3000, 400, 0.0011, 1500,
                   CASE WHEN status = 'extraction_failed' THEN 'failed' ELSE 'succeeded' END, created_at
            FROM gen
        """), params)


if __name__ == "__main__":
    main()
