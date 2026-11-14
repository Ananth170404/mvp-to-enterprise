"""Run the invoice list's real queries with EXPLAIN ANALYZE (guide Chapter 5).

    docker compose run --rm api python -m scripts.explain_queries
    docker compose run --rm api python -m scripts.explain_queries --plans    # full plans

The queries come from app/queries.py, the same functions the app uses, run
for the tenant with the most invoices (the worst case). For each: execution
time, how it reads the table (Seq Scan = every row; Index Scan / Index Only
Scan = just the rows it needs), and how much data it touched.

Then the database's own view: the slowest statements by total time, from
pg_stat_statements (what's ACTUALLY slow in a running system, rather than what
you guessed).
"""

import argparse
import re

from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql

from app import queries
from app.config import database_url_from_env, settings

SCAN = re.compile(r"(Parallel )?(Seq Scan|Index Only Scan|Index Scan( Backward)?|Bitmap Heap Scan)( using \w+| on \w+)")


def sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plans", action="store_true", help="print full plans")
    parser.add_argument("--reset", action="store_true", help="clear pg_stat_statements, then exit (start a fresh measurement)")
    args = parser.parse_args()

    engine = create_engine(database_url_from_env("MIGRATION_") or settings.database_url)
    with engine.connect() as conn:
        # Needs the library preloaded (docker-compose.yml; on by default on RDS).
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
        conn.commit()
        if args.reset:
            conn.execute(text("SELECT pg_stat_statements_reset()"))
            conn.commit()
            print("pg_stat_statements cleared. Use the app, then run this again without --reset.")
            return
        tenant_id, invoices = conn.execute(text(
            "SELECT tenant_id, count(*) FROM invoices GROUP BY tenant_id ORDER BY 2 DESC LIMIT 1"
        )).one()
        vendor = conn.scalar(text("SELECT vendor_tax_id FROM invoices WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id})
        print(f"Largest tenant: {invoices:,} invoices\n")

        checks = {
            "List page: newest 200": queries.invoice_page(tenant_id),
            "List page: newest 200 needing review": queries.invoice_page(tenant_id, "needs_review"),
            "Status counts (the tabs)": queries.status_counts(tenant_id),
            "Total LLM spend": queries.total_spend(tenant_id),
            "Duplicate check (same vendor, number)": queries.possible_duplicates(tenant_id, "x", vendor)
                .where(queries.Invoice.invoice_number == "INV-123"),
            "Duplicate check (same vendor, amount, date)": queries.possible_duplicates(tenant_id, "x", vendor)
                .where(queries.Invoice.total_minor == 123400, queries.Invoice.invoice_date == "2026-01-15"),
        }
        print(f"{'query':46} {'time':>10}  access")
        for name, statement in checks.items():
            plan = [r[0] for r in conn.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql(statement)))]
            total = next(float(m.group(1)) for line in plan if (m := re.search(r"Execution Time: ([\d.]+)", line)))
            scans = sorted({m.group(0) for line in plan for m in SCAN.finditer(line)})
            print(f"{name:46} {total:8.1f} ms  {'; '.join(scans)}")
            if args.plans:
                print("\n".join("      " + line for line in plan) + "\n")

        has_pss = conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'"))
        if not has_pss:
            print("\npg_stat_statements isn't enabled: CREATE EXTENSION pg_stat_statements; (needs it in shared_preload_libraries)")
            return
        print("\nSlowest statements by total time (pg_stat_statements):")
        rows = conn.execute(text("""
            SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time::numeric, 1) AS mean_ms,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 90) AS query
            FROM pg_stat_statements
            WHERE query NOT ILIKE '%pg_stat_statements%' AND query NOT ILIKE 'EXPLAIN%'
            ORDER BY total_exec_time DESC LIMIT 8
        """)).all()
        for r in rows:
            print(f"  {r.calls:>6} calls  {r.total_ms:>8} ms total  {r.mean_ms:>7} ms avg  {r.query}")


if __name__ == "__main__":
    main()
