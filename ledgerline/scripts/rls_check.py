"""Prove the database keeps tenants apart, even when the app's code doesn't (guide Chapter 7).

    docker compose run --rm api python -m scripts.rls_check

Creates two throwaway tenants with one invoice each (as the schema owner), then
connects as the APP's database user, the one a bug or an injection would be
running as, and tries what a bug would do:

    a query that forgot its tenant filter         only the current tenant's rows
    no tenant set at all                          no rows
    another tenant's invoice, by its ID           not found
    changing another tenant's invoice             0 rows changed
    writing a row into another tenant             refused by the database
    another tenant's invoice lines (child table)  none

Then cleans up. Exits 1 if any check fails, so CI runs it on every push. Needs
Postgres (row-level security doesn't exist in SQLite) and the app user from
`python -m app.db_roles`.
"""

import sys
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.config import database_url_from_env, settings
from app.db import tenant_scope


def main() -> None:
    owner_url = database_url_from_env("MIGRATION_")
    if not owner_url or not settings.database_url.startswith("postgresql"):
        sys.exit("Needs Postgres, with DATABASE_URL (app user) and MIGRATION_DATABASE_URL (schema owner).")
    owner, app = create_engine(owner_url), create_engine(settings.database_url)
    tag = uuid.uuid4().hex[:8]
    a, b = _make_tenant(owner, f"RLS check A {tag}"), _make_tenant(owner, f"RLS check B {tag}")
    results: list[tuple[str, str, bool]] = []

    def check(name: str, seen, ok: bool) -> None:
        results.append((name, str(seen), ok))

    try:
        with app.connect() as conn:
            user, bypass = conn.execute(text(
                "SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
        check("app connects as a user without BYPASSRLS", f"{user}, bypassrls={bypass}", not bypass)

        with app.begin() as conn:  # the "forgot the filter" query: SELECT ... FROM invoices, no WHERE tenant_id
            tenant_scope(conn, a["tenant"])
            ids = set(conn.scalars(text("SELECT id FROM invoices WHERE invoice_number LIKE :t"), {"t": f"%{tag}"}))
        check("unfiltered query, as tenant A", f"{len(ids)} invoice(s)", ids == {a["invoice"]})

        with app.begin() as conn:
            n = conn.scalar(text("SELECT count(*) FROM invoices"))
        check("no tenant set", f"{n} invoices", n == 0)

        with app.begin() as conn:
            tenant_scope(conn, a["tenant"])
            row = conn.execute(text("SELECT id FROM invoices WHERE id = :id"), {"id": b["invoice"]}).first()
        check("tenant B's invoice by ID, as tenant A", "found" if row else "not found", row is None)

        with app.begin() as conn:
            tenant_scope(conn, a["tenant"])
            changed = conn.execute(text("UPDATE invoices SET status = 'rejected' WHERE id = :id"),
                                   {"id": b["invoice"]}).rowcount
        check("change tenant B's invoice, as tenant A", f"{changed} row(s) changed", changed == 0)

        refused = False
        try:
            with app.begin() as conn:
                tenant_scope(conn, a["tenant"])
                conn.execute(text("UPDATE invoices SET tenant_id = :b WHERE id = :id"),
                             {"b": b["tenant"], "id": a["invoice"]})
        except DBAPIError as exc:
            refused = "row-level security" in str(exc)
        check("move tenant A's invoice into tenant B", "refused" if refused else "ALLOWED", refused)

        with app.begin() as conn:
            tenant_scope(conn, a["tenant"])
            lines = conn.scalar(text("SELECT count(*) FROM invoice_lines WHERE invoice_id = :id"),
                                {"id": b["invoice"]})
        check("tenant B's invoice lines, as tenant A", f"{lines} line(s)", lines == 0)

        with owner.connect() as conn:  # for contrast: the owner bypasses the policies
            n = conn.scalar(text("SELECT count(*) FROM invoices WHERE invoice_number LIKE :t"), {"t": f"%{tag}"})
        check("for contrast: the schema owner sees both", f"{n} invoices", n == 2)
    finally:
        _clean_up(owner, [a, b])

    width = max(len(r[0]) for r in results)
    for name, seen, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {seen}")
    if not all(ok for *_, ok in results):
        sys.exit(1)
    print("Tenant isolation holds in the database.")


def _make_tenant(owner, name: str) -> dict:
    ids = {k: str(uuid.uuid4()) for k in ("tenant", "user", "document", "invoice", "line")}
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO tenants (id, name, created_at) VALUES (:tenant, :name, now())"),
                     {**ids, "name": name})
        conn.execute(text("""INSERT INTO users (id, tenant_id, email, name, role)
                             VALUES (:user, :tenant, :email, 'RLS check', 'clerk')"""),
                     {**ids, "email": f"rls-{ids['user']}@check.example"})
        conn.execute(text("""INSERT INTO documents (id, tenant_id, storage_key, sha256, mime_type, original_filename,
                                                    size_bytes, uploaded_by, created_at)
                             VALUES (:document, :tenant, 'rls-check', :document, 'application/pdf', 'x.pdf', 1,
                                     :user, now())"""), ids)
        conn.execute(text("""INSERT INTO invoices (id, tenant_id, document_id, status, status_changed_at, currency,
                                                   invoice_number, flags, created_at, updated_at)
                             VALUES (:invoice, :tenant, :document, 'needs_review', now(), 'INR', :number, '[]',
                                     now(), now())"""), {**ids, "number": f"RLS-{name[-8:]}"})
        conn.execute(text("""INSERT INTO invoice_lines (id, invoice_id, position, description, quantity,
                                                        unit_price_minor, amount_minor)
                             VALUES (:line, :invoice, 1, 'check', 1, 100, 100)"""), ids)
    return ids


def _clean_up(owner, tenants: list[dict]) -> None:
    with owner.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM invoice_lines WHERE invoice_id = :invoice"), t)
            conn.execute(text("DELETE FROM invoices WHERE id = :invoice"), t)
            conn.execute(text("DELETE FROM documents WHERE id = :document"), t)
            conn.execute(text("DELETE FROM users WHERE id = :user"), t)
            conn.execute(text("DELETE FROM tenants WHERE id = :tenant"), t)


if __name__ == "__main__":
    main()
