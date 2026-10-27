-- Least-privilege database users (guide Chapter 2, "Config and secrets").
--
--   ledgerline       owns the schema; used ONLY by migrations (MIGRATION_DATABASE_URL)
--   ledgerline_app   used by the running app (DATABASE_URL): can read and write
--                    rows, cannot create, alter or drop tables
--
-- A bug or an injection in the app can then damage rows, but can't drop the
-- invoices table or disable the audit-log trigger.
--
-- Run after migrations, as the schema owner (needs CREATEROLE; the RDS master
-- user and the Docker POSTGRES_USER both have it). Safe to re-run.
--   python -m app.db_roles        (reads MIGRATION_DATABASE_URL and APP_DB_PASSWORD)
--   psql "$URL" -v app_password="'...'" -f scripts/db_roles.sql   (same thing by hand)

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerline_app') THEN
        CREATE ROLE ledgerline_app LOGIN;
    END IF;
END
$$;

ALTER ROLE ledgerline_app PASSWORD :app_password;

-- Whatever the database is called in this environment (it differs on RDS).
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ledgerline_app', current_database());
END
$$;
GRANT USAGE ON SCHEMA public TO ledgerline_app;
REVOKE CREATE ON SCHEMA public FROM ledgerline_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ledgerline_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledgerline_app;

-- Tables created by future migrations (run as ledgerline) get the same grants.
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerline IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledgerline_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerline IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ledgerline_app;

-- The audit log is append-only for the app at the permission level too,
-- on top of the trigger.
REVOKE UPDATE, DELETE ON audit_events FROM ledgerline_app;
-- Nor should the app rewrite migration history.
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM ledgerline_app;
