"""connections a tenant may check, and the evidence checks produce

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def _id() -> sa.Column[object]:
    return sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant() -> sa.Column[object]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "connections",
        _id(),
        _tenant(),
        sa.Column("system", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # One tenant per scope, across the whole deployment (see models.Connection).
        sa.UniqueConstraint("system", "scope", name="uq_connections_scope"),
    )
    op.create_table(
        "evidence",
        _id(),
        _tenant(),
        sa.Column("check_name", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(300), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("raw_result", postgresql.JSONB(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evidence_lookup", "evidence", ["tenant_id", "check_name", "scope", "observed_at"]
    )


def downgrade() -> None:
    op.drop_table("evidence")
    op.drop_table("connections")
