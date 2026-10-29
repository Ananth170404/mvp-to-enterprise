import sys
import time
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.config import database_url_from_env, settings
from app.db import Base

config = context.config
# Migrations can run as a different database user from the app: the app's user
# can read and write rows but not change the schema (least privilege).
DATABASE_URL = database_url_from_env("MIGRATION_") or settings.database_url
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

# Arbitrary constant: every migration run takes the same Postgres advisory lock,
# so two runs started at once (two deploys, two instances) can't interleave.
MIGRATION_LOCK_ID = 7_315_002

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_postgres = connection.dialect.name == "postgresql"
        if is_postgres:
            # Poll with pg_try_advisory_lock instead of blocking in pg_advisory_lock.
            # A blocked call holds an open transaction, and CREATE INDEX CONCURRENTLY
            # in the other run waits for every open transaction to finish: deadlock.
            # Committing between attempts means we wait without holding anything.
            waited = 0
            while not connection.exec_driver_sql(f"SELECT pg_try_advisory_lock({MIGRATION_LOCK_ID})").scalar():
                connection.commit()
                if waited % 10 == 0:
                    print(f"Another migration is running; waiting ({waited}s)", file=sys.stderr)
                time.sleep(1)
                waited += 1
            connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_postgres:
                connection.exec_driver_sql(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
