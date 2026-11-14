"""questionnaires, their original files, and their questions

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
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
        "questionnaires",
        _id(),
        _tenant(),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("mapping", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_questionnaires_tenant_sha256"),
    )
    op.create_table(
        "questionnaire_files",
        sa.Column(
            "questionnaire_id",
            _UUID,
            sa.ForeignKey("questionnaires.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _tenant(),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
    )
    op.create_table(
        "questions",
        _id(),
        _tenant(),
        sa.Column(
            "questionnaire_id",
            _UUID,
            sa.ForeignKey("questionnaires.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sheet", sa.Text(), nullable=False),
        sa.Column("cell", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("answer_cell", sa.String(16)),
        sa.Column("external_id", sa.String(255)),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Also the index that serves "questions of one questionnaire, in order".
        sa.UniqueConstraint("questionnaire_id", "position", name="uq_questions_position"),
    )


def downgrade() -> None:
    op.drop_table("questions")
    op.drop_table("questionnaire_files")
    op.drop_table("questionnaires")
