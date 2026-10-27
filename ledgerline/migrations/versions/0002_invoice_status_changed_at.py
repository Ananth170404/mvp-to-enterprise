"""invoice status_changed_at

Revision ID: 0002
Revises: 0001

A production-safe column addition (guide Chapter 2, "expand and contract"):

1. Add the column as NULLABLE. Instant in Postgres: no table rewrite, and the
   code that is still running (which doesn't know the column) keeps working.
2. Backfill existing rows in batches, so no single statement locks the table
   for long.
3. Build the index CONCURRENTLY, so writes aren't blocked while it builds.

Making the column NOT NULL would be a later, separate migration, once every
running version of the code sets it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

BATCH = 5_000


def upgrade() -> None:
    op.add_column("invoices", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE invoices SET status_changed_at = updated_at
                    WHERE id IN (
                        SELECT id FROM invoices WHERE status_changed_at IS NULL LIMIT :batch
                    )
                    """
                ),
                {"batch": BATCH},
            )
            if result.rowcount < BATCH:
                break
        # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
        with op.get_context().autocommit_block():
            op.create_index(
                "ix_invoices_status_status_changed_at",
                "invoices",
                ["status", "status_changed_at"],
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    else:
        bind.execute(sa.text("UPDATE invoices SET status_changed_at = updated_at WHERE status_changed_at IS NULL"))
        op.create_index("ix_invoices_status_status_changed_at", "invoices", ["status", "status_changed_at"])


def downgrade() -> None:
    op.drop_index("ix_invoices_status_status_changed_at", table_name="invoices")
    op.drop_column("invoices", "status_changed_at")
