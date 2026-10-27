"""Backup and restore drill: prove a backup restores, and time it.

A backup you have never restored is a hope, not a backup. This script:

  1. takes a logical backup of the database (pg_dump, custom format)
  2. restores it into a brand-new scratch database
  3. checks the restore: every table's row count, the schema version, the
     audit-log protection trigger, and that every uploaded document the
     database refers to still exists in file storage
  4. reports how long it took: your recovery time (RTO) at today's data size
  5. drops the scratch database (unless --keep)

    python -m scripts.backup_drill                # uses MIGRATION_DATABASE_URL
    python -m scripts.backup_drill --keep --out /tmp/ledgerline.dump

Needs pg_dump and pg_restore at least as new as the server (the dev Docker
image has them). On AWS the same idea applies to RDS snapshots and
point-in-time recovery; guide Chapter 2 covers the drill there.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def with_database(url: str, name: str) -> str:
    base, _, rest = url.rpartition("/")
    query = rest.partition("?")[2]
    return f"{base}/{name}" + (f"?{query}" if query else "")


def table_counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )]
        return {
            t: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
            for t in tables
        }


def check(name: str, ok: bool, detail: str, results: list) -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL"))
    parser.add_argument("--out", type=Path, default=None, help="Keep the dump file here")
    parser.add_argument("--keep", action="store_true", help="Don't drop the scratch database afterwards")
    parser.add_argument("--skip-files", action="store_true", help="Don't check document files in storage")
    args = parser.parse_args()

    if not args.source or not args.source.startswith("postgresql"):
        sys.exit("Needs a Postgres URL in --source, MIGRATION_DATABASE_URL or DATABASE_URL.")
    pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        sys.exit("pg_dump and pg_restore must be on PATH.")

    source = libpq(args.source)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    scratch_name = f"ledgerline_restore_drill_{stamp}"
    scratch = with_database(source, scratch_name)
    dump = args.out or Path(tempfile.gettempdir()) / f"ledgerline-{stamp}.dump"
    results: list = []

    print(f"Backing up {source.rpartition('@')[2]} ...")
    t0 = time.monotonic()
    subprocess.run([pg_dump, "--format=custom", "--no-owner", "--file", str(dump), source], check=True)
    dump_s = time.monotonic() - t0
    print(f"  {dump.stat().st_size / 1024:.0f} KB in {dump_s:.1f} s")

    print(f"Restoring into new database {scratch_name} ...")
    with psycopg.connect(source, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch_name)))
    try:
        t1 = time.monotonic()
        subprocess.run(
            [pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "--jobs", "4", "--dbname", scratch, str(dump)],
            check=True,
        )
        restore_s = time.monotonic() - t1
        print(f"  restored in {restore_s:.1f} s")

        print("Verifying the restore ...")
        before, after = table_counts(source), table_counts(scratch)
        mismatched = {t: (before.get(t), after.get(t)) for t in before.keys() | after.keys() if before.get(t) != after.get(t)}
        check("row counts", not mismatched,
              f"{len(before)} tables, {sum(before.values())} rows match" if not mismatched else f"differ: {mismatched}", results)

        with psycopg.connect(source) as s_conn, psycopg.connect(scratch) as r_conn:
            v_src = s_conn.execute("SELECT version_num FROM alembic_version").fetchone()
            v_new = r_conn.execute("SELECT version_num FROM alembic_version").fetchone()
            check("schema version", v_src == v_new, f"{v_new[0] if v_new else None}", results)
            trigger = r_conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = 'audit_events_no_update_delete' AND NOT tgisinternal"
            ).fetchone()
            check("audit log protection", trigger is not None,
                  "append-only trigger present" if trigger else "trigger MISSING: restored audit log is editable", results)
            keys = [r[0] for r in r_conn.execute("SELECT storage_key FROM documents")]

        if not args.skip_files:
            from app.storage import storage

            missing = [k for k in keys if not storage.exists(k)]
            check("document files", not missing,
                  f"all {len(keys)} referenced files exist" if not missing else
                  f"{len(missing)} of {len(keys)} missing, e.g. {missing[0]}", results)
    finally:
        if not args.keep:
            with psycopg.connect(source, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(scratch_name)))
        if args.out is None:
            dump.unlink(missing_ok=True)

    failed = [name for name, ok, _ in results if not ok]
    total = dump_s + restore_s
    print()
    print(f"Backup {dump_s:.1f} s + restore {restore_s:.1f} s = {total:.1f} s at this data size.")
    print("That restore time, plus however long it takes a person to notice and start, is your real RTO.")
    print("RESULT: " + ("PASS" if not failed else f"FAIL ({', '.join(failed)})"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
