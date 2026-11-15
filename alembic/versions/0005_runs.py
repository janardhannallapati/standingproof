"""runs over a questionnaire, and each question's place in one

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TIME = sa.DateTime(timezone=True)


def _id() -> sa.Column[object]:
    return sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant() -> sa.Column[object]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _ref(name: str, target: str) -> sa.Column[object]:
    return sa.Column(name, _UUID, sa.ForeignKey(target, ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "runs",
        _id(),
        _tenant(),
        _ref("questionnaire_id", "questionnaires.id"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", _TIME, nullable=True),
    )
    # At most one active run per questionnaire: a second would pay twice for the same answers.
    op.create_index(
        "uq_runs_one_active",
        "runs",
        ["questionnaire_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "run_questions",
        _id(),
        _tenant(),
        _ref("run_id", "runs.id"),
        _ref("question_id", "questions.id"),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("path", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_until", _TIME, nullable=True),
        sa.Column("enqueue_after", _TIME, nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", _TIME, nullable=True),
        sa.Column("finished_at", _TIME, nullable=True),
        sa.Column("state_deleted_at", _TIME, nullable=True),
        sa.UniqueConstraint("run_id", "question_id", name="uq_run_questions_question"),
    )
    op.create_index("ix_run_questions_run_status", "run_questions", ["run_id", "status"])
    op.create_index(
        "ix_run_questions_open",
        "run_questions",
        ["status", "enqueue_after"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("run_questions")
    op.drop_table("runs")
