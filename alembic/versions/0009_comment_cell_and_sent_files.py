"""a comments cell per question, and a record of files sent to customers

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # Nullable with no default: questionnaires uploaded before this have no comments cell
    # recorded and behave as they did.
    op.add_column("questions", sa.Column("comment_cell", sa.String(16), nullable=True))
    op.create_table(
        "sent_files",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("run_id", _UUID, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "filled_file_id",
            _UUID,
            sa.ForeignKey("filled_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer", sa.String(200), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=False),
        sa.Column("sent_by", sa.String(200), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "uq_sent_files_file_customer",
        "sent_files",
        ["filled_file_id", sa.text("lower(customer)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_sent_files_file_customer", table_name="sent_files")
    op.drop_table("sent_files")
    op.drop_column("questions", "comment_cell")
