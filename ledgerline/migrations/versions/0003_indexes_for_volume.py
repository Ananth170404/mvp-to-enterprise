"""indexes for the invoice list and duplicate checks at volume

Revision ID: 0003
Revises: 0002

Found by loading a million invoices and running the app's own queries with
EXPLAIN ANALYZE (guide Chapter 5): the list page, its status counts, the spend
total and the duplicate check all read every row of their table.

Each index matches a query's shape: equality columns first, then the column
it sorts or ranges on.

  invoices (tenant_id, created_at)         the list page: one tenant, newest first
  invoices (tenant_id, status, created_at) the list filtered by status
  invoices (tenant_id, vendor_tax_id)      duplicate checks on every extraction
  extraction_runs (tenant_id) INCLUDE (cost_usd)
                                           lets the spend total be read from the
                                           index alone (an index-only scan)

What these do NOT fix: the status counts and the spend total. Both still read
one index entry per invoice (700,000 for the largest tenant), indexed or not.
Counting many rows needs a different design: a summary kept up to date, a
cache, or an estimate (guide 5.5).

Built CONCURRENTLY: no lock that blocks writes while they build (minutes on a
big table). The cost: CONCURRENTLY can't run in a transaction, and a failed
build leaves an INVALID index behind, which must be dropped and rebuilt.

The single-column indexes these make redundant (ix_invoices_tenant_id,
ix_invoices_status, ix_extraction_runs_tenant_id) are NOT dropped here:
dropping them is a separate, later "contract" migration, once these have proved
themselves in production (guide Chapter 2, expand and contract).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_invoices_tenant_id_created_at", "invoices", ["tenant_id", "created_at"], {}),
    ("ix_invoices_tenant_id_status_created_at", "invoices", ["tenant_id", "status", "created_at"], {}),
    ("ix_invoices_tenant_id_vendor_tax_id", "invoices", ["tenant_id", "vendor_tax_id"], {}),
    ("ix_extraction_runs_tenant_id_cost", "extraction_runs", ["tenant_id"], {"postgresql_include": ["cost_usd"]}),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
        with op.get_context().autocommit_block():
            for name, table, columns, extra in INDEXES:
                op.create_index(name, table, columns, postgresql_concurrently=True, if_not_exists=True, **extra)
    else:
        for name, table, columns, _ in INDEXES:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
