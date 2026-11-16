"""a reviewer's decisions, and each question's review state

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("run_questions", sa.Column("review", sa.String(16), nullable=True))
    op.add_column("run_questions", sa.Column("final_answer", sa.Text(), nullable=True))
    op.add_column(
        "run_questions",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "run_questions",
        sa.Column("redrafts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("run_questions", sa.Column("reviewed_at", _TIME, nullable=True))
    # Questions that finished before this migration were waiting for a person all along.
    op.execute(
        "UPDATE run_questions SET review = 'pending' WHERE status IN ('ready', 'needs_review')"
    )
    op.create_table(
        "review_decisions",
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
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("shown_answer", sa.Text(), nullable=True),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("batch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("decided_by", sa.String(200), nullable=False),
        sa.Column("decided_at", _TIME, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_review_decisions_question", "review_decisions", ["run_question_id", "decided_at"]
    )


def downgrade() -> None:
    op.drop_table("review_decisions")
    for column in ("reviewed_at", "redrafts", "version", "final_answer", "review"):
        op.drop_column("run_questions", column)
