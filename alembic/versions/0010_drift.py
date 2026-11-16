"""drift: what an answer rests on, changes seen, answers reopened, webhook deliveries

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_NEW_ID = sa.text("gen_random_uuid()")
_NOW = sa.text("now()")


def _fk(table: str, *, nullable: bool = False) -> sa.Column[object]:
    name = {"tenants": "tenant_id"}[table]
    return sa.Column(
        name, _UUID, sa.ForeignKey(f"{table}.id", ondelete="CASCADE"), nullable=nullable
    )


def _evidence(name: str, *, nullable: bool = False, **more: object) -> sa.Column[object]:
    return sa.Column(
        name, _UUID, sa.ForeignKey("evidence.id", ondelete="CASCADE"), nullable=nullable, **more
    )


def _question(**more: object) -> sa.Column[object]:
    return sa.Column(
        "run_question_id",
        _UUID,
        sa.ForeignKey("run_questions.id", ondelete="CASCADE"),
        nullable=False,
        **more,
    )


def _when(name: str, *, nullable: bool = False) -> sa.Column[object]:
    default = None if nullable else _NOW
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def upgrade() -> None:
    op.add_column("run_questions", _when("reopened_at", nullable=True))

    op.create_table(
        "answer_evidence",
        _question(primary_key=True),
        sa.Column("check_name", sa.String(100), primary_key=True),
        sa.Column("scope", sa.String(300), primary_key=True),
        _fk("tenants"),
        _evidence("evidence_id"),
    )
    op.create_index(
        "ix_answer_evidence_setting", "answer_evidence", ["tenant_id", "check_name", "scope"]
    )
    # Answers drafted before this migration rest on evidence too: link each to the newest
    # observation it cites, per setting. A citation that is not an observation of this
    # tenant's (a library answer, a failed look, anything malformed) matches nothing.
    op.execute(
        """
        INSERT INTO answer_evidence (run_question_id, check_name, scope, tenant_id, evidence_id)
        SELECT DISTINCT ON (rq.id, e.check_name, e.scope)
               rq.id, e.check_name, e.scope, rq.tenant_id, e.id
        FROM run_questions rq
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(rq.citations) = 'array' THEN rq.citations ELSE '[]'::jsonb END
        ) AS cited(source)
        JOIN evidence e
          ON cited.source = 'evidence:' || e.id::text
         AND e.tenant_id = rq.tenant_id
         AND e.status = 'observed'
        ORDER BY rq.id, e.check_name, e.scope, e.observed_at DESC
        """
    )

    op.create_table(
        "drift_queue",
        _evidence("evidence_id", primary_key=True),
        _fk("tenants"),
        sa.Column("trigger", sa.String(16), nullable=False),
        _when("queued_at"),
    )

    op.create_table(
        "drift_events",
        sa.Column("id", _UUID, primary_key=True, server_default=_NEW_ID),
        _fk("tenants"),
        sa.Column("check_name", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(300), nullable=False),
        _evidence("evidence_id", unique=True),
        _evidence("previous_evidence_id", nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        _when("detected_at"),
    )
    op.create_index("ix_drift_events_tenant", "drift_events", ["tenant_id", "detected_at"])

    op.create_table(
        "answer_reopenings",
        sa.Column("id", _UUID, primary_key=True, server_default=_NEW_ID),
        _fk("tenants"),
        sa.Column(
            "drift_event_id",
            _UUID,
            sa.ForeignKey("drift_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _question(),
        _evidence("was_evidence_id"),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.Column("was_review", sa.String(16), nullable=True),
        _when("reopened_at"),
        sa.UniqueConstraint("drift_event_id", "run_question_id", name="uq_answer_reopenings_once"),
    )
    op.create_index(
        "ix_answer_reopenings_question", "answer_reopenings", ["run_question_id", "reopened_at"]
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", _UUID, primary_key=True, server_default=_NEW_ID),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("delivery_id", sa.String(100), nullable=False),
        sa.Column("event", sa.String(60), nullable=False),
        sa.Column("action", sa.String(40), nullable=True),
        _fk("tenants", nullable=True),
        sa.Column("check_name", sa.String(100), nullable=True),
        sa.Column("scope", sa.String(300), nullable=True),
        _when("received_at"),
        _when("handled_at", nullable=True),
        sa.Column("outcome", sa.String(60), nullable=True),
        sa.UniqueConstraint("source", "delivery_id", name="uq_webhook_deliveries_delivery"),
    )
    op.create_index(
        "ix_webhook_deliveries_waiting",
        "webhook_deliveries",
        ["received_at"],
        postgresql_where=sa.text("handled_at IS NULL"),
    )


def downgrade() -> None:
    for table in (
        "webhook_deliveries",
        "answer_reopenings",
        "drift_events",
        "drift_queue",
        "answer_evidence",
    ):
        op.drop_table(table)
    op.drop_column("run_questions", "reopened_at")
