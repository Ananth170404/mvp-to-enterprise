"""Create or refresh the app's least-privilege database user.

Runs scripts/db_roles.sql as the schema owner (MIGRATION_DATABASE_URL, or
DB_HOST + MIGRATION_DB_USER + MIGRATION_DB_PASSWORD), setting the app user's
password from DB_PASSWORD (or APP_DB_PASSWORD). Safe to run on every deploy.

    python -m app.db_roles
"""

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from app.config import database_url_from_env

SQL_FILE = Path(__file__).resolve().parent.parent / "scripts" / "db_roles.sql"


def main() -> None:
    url = database_url_from_env("MIGRATION_")
    password = os.getenv("APP_DB_PASSWORD") or os.getenv("DB_PASSWORD")
    if not url or not password:
        sys.exit("Set the schema owner's connection (MIGRATION_DATABASE_URL or MIGRATION_DB_*) and APP_DB_PASSWORD or DB_PASSWORD.")
    url = url.replace("postgresql+psycopg://", "postgresql://")

    script = SQL_FILE.read_text()
    with psycopg.connect(url, autocommit=True) as conn:
        literal = sql.Literal(password).as_string(conn)  # quoted safely, never string-formatted
        conn.execute(script.replace(":app_password", literal))
    print("Database roles applied: ledgerline_app can read/write rows only.")


if __name__ == "__main__":
    main()
