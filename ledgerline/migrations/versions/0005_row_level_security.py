"""row-level security for tenant data

Revision ID: 0005
Revises: 0004

The second wall between tenants (guide Chapter 7). The first is the app: every
query filters by the signed-in user's tenant. This one is in the database:
for the app's database user, every tenant-owned table only has the rows whose
tenant_id matches the transaction's app.tenant_id setting (app/db.py sets it).
A query that forgets its tenant filter, or an injected one, then finds only
the current tenant's rows, or none at all.

    tenant-owned (policy on tenant_id):  documents, invoices, extraction_runs,
                                         purchase_orders, vendors, audit_events
    via their parent (policy by EXISTS): invoice_lines, po_lines
    not scoped:                          tenants, users, sessions, login_requests
                                         (read at sign-in, before the tenant is known;
                                         protected by the app's own checks)

USING decides which rows are visible (SELECT, UPDATE, DELETE); WITH CHECK which
rows may be written (INSERT, UPDATE): you can't move a row into another tenant
either.

Not FORCEd: the table owner (ledgerline, used only by migrations, seeding and
volume tests) bypasses the policies, as owners do by default. The app's user
(ledgerline_app) is not the owner and has no BYPASSRLS, so the policies always
apply to it. Postgres only; SQLite has no row-level security.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TENANT_TABLES = ["documents", "invoices", "extraction_runs", "purchase_orders", "vendors", "audit_events"]
CHILD_TABLES = {"invoice_lines": ("invoice_id", "invoices"), "po_lines": ("po_id", "purchase_orders")}

# current_setting(..., true) returns NULL (or '') when unset: no rows match.
CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT})"
        )
    for table, (fk, parent) in CHILD_TABLES.items():
        # The parent is itself filtered by its policy, so this sees only the
        # current tenant's parents.
        exists = f"EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk})"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({exists}) WITH CHECK ({exists})")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in [*TENANT_TABLES, *CHILD_TABLES]:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
