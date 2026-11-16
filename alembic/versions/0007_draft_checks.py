"""what the independent checker made of each drafted wording

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "draft_checks",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_question_id",
            _UUID,
            sa.ForeignKey("run_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("answer_checked", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_draft_checks_question", "draft_checks", ["run_question_id", "checked_at"])


def downgrade() -> None:
    op.drop_index("ix_draft_checks_question", table_name="draft_checks")
    op.drop_table("draft_checks")
