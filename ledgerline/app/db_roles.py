"""Create or refresh the app's least-privilege database user.

Runs scripts/db_roles.sql as the schema owner (MIGRATION_DATABASE_URL), with
the app user's password from APP_DB_PASSWORD. Safe to run on every deploy.

    python -m app.db_roles
"""

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

SQL_FILE = Path(__file__).resolve().parent.parent / "scripts" / "db_roles.sql"


def main() -> None:
    url = os.getenv("MIGRATION_DATABASE_URL")
    password = os.getenv("APP_DB_PASSWORD")
    if not url or not password:
        sys.exit("Set MIGRATION_DATABASE_URL and APP_DB_PASSWORD.")
    url = url.replace("postgresql+psycopg://", "postgresql://")

    script = SQL_FILE.read_text()
    with psycopg.connect(url, autocommit=True) as conn:
        literal = sql.Literal(password).as_string(conn)  # quoted safely, never string-formatted
        conn.execute(script.replace(":app_password", literal))
    print("Database roles applied: ledgerline_app can read/write rows only.")


if __name__ == "__main__":
    main()
