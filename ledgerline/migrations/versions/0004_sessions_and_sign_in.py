"""sessions and sign-in

Revision ID: 0004
Revises: 0003

Real sign-in (guide Chapter 7):
  * users.oidc_subject: the identity provider's permanent ID for the person,
    linked on first sign-in; users.last_login_at
  * sessions: one row per signed-in browser (only a hash of the cookie's token)
  * login_requests: sign-ins in progress (state, nonce, PKCE verifier)

All additive: the running code ignores the new tables and columns, so this can
deploy before the code that uses them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oidc_subject", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_users_oidc_subject", "users", ["oidc_subject"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("id_token", sa.Text(), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "login_requests",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("return_to", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("login_requests")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_constraint("uq_users_oidc_subject", "users", type_="unique")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "oidc_subject")
