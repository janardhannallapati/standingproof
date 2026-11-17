"""decisions, checks, files, sent records and reopenings are numbered in the order stored

Revision ID: 0018
Revises: 0017

"The latest decision", "the first reopening" and "the current file" were chosen by a
timestamp. A clock is stepped back now and then, and two hosts never agree to the second,
so the row stored last could carry the earlier time: the model was then sent an older
redraft instruction, and a file was refused because an older decision looked newer.

Each of the five append-only tables that lacked one gets `seq`, from a sequence, as
`evidence` has had since 0011. A sequence gives numbers in the order rows are stored and
never goes back. The timestamps stay, to show a person when something happened.

`run_questions.reopened_seq` is the number of the first reopening since the last decision.
"Which reopenings belong to this wait" was `reopened_at >= run_questions.reopened_at`, a
comparison of two clock readings; it is now a comparison of two numbers.

The app role cannot choose a row's place: INSERT is given column by column without `seq`
(whole-table INSERT would allow OVERRIDING SYSTEM VALUE), and it holds no UPDATE on these
tables at all (0015).
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

APP = "standingproof_app"

# table: the timestamp that gave the order until now.
NUMBERED = {
    "review_decisions": "decided_at",
    "draft_checks": "checked_at",
    "filled_files": "created_at",
    "sent_files": "sent_at",
    "answer_reopenings": "reopened_at",
}
# The indexes that find "the latest for this question".
INDEXES = {
    "review_decisions": "ix_review_decisions_question",
    "draft_checks": "ix_draft_checks_question",
    "answer_reopenings": "ix_answer_reopenings_question",
}


def _as_plain_owner(table: str, statement: str) -> None:
    """Run a statement that touches rows. The policies are forced, so they bind an owner
    that is not a superuser; lifted for this statement and put back."""
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(statement)
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    for table, stamped in NUMBERED.items():
        op.add_column(table, sa.Column("seq", sa.BigInteger(), nullable=True))
        # Existing rows keep the order they had until now. Names from the constants above.
        _as_plain_owner(
            table,
            f"""
            UPDATE {table} SET seq = ordered.n
            FROM (SELECT id, row_number() OVER (ORDER BY {stamped}, id) AS n FROM {table}) ordered
            WHERE {table}.id = ordered.id
            """,  # noqa: S608
        )
        op.alter_column(table, "seq", nullable=False)
        # ALWAYS: the application cannot choose a row's place in the order.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY")
        _as_plain_owner(
            table,
            f"SELECT setval(pg_get_serial_sequence('{table}', 'seq'), "  # noqa: S608
            f"(SELECT coalesce(max(seq), 0) + 1 FROM {table}), false)",
        )
        # INSERT on the whole table would let a statement name `seq` itself.
        op.execute(f"REVOKE INSERT ON {table} FROM {APP}")
        op.execute(
            f"""
            DO $$
            DECLARE columns text;
            BEGIN
              SELECT string_agg(format('%I', column_name), ', ') INTO columns
              FROM information_schema.columns
              WHERE table_schema = 'public' AND table_name = '{table}' AND column_name <> 'seq';
              EXECUTE format('GRANT INSERT (%s) ON {table} TO {APP}', columns);
            END $$
            """  # noqa: S608
        )
    for table, index in INDEXES.items():
        op.drop_index(index, table)
        op.create_index(index, table, ["run_question_id", "seq"])

    op.add_column("run_questions", sa.Column("reopened_seq", sa.BigInteger(), nullable=True))
    op.execute(f"GRANT UPDATE (reopened_seq) ON run_questions TO {APP}")
    # An answer waiting now: its first reopening by the rule used until today. Where that
    # rule found none, every reopening of the question counts (0 is below any number).
    for table in ("answer_reopenings", "run_questions"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE run_questions q SET reopened_seq = coalesce((
          SELECT min(r.seq) FROM answer_reopenings r
          WHERE r.run_question_id = q.id AND r.reopened_at >= q.reopened_at), 0)
        WHERE q.reopened_at IS NOT NULL
        """
    )
    for table in ("answer_reopenings", "run_questions"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    # Both or neither, kept by the database. Code from before this migration sets only the
    # time; it is refused, loudly, where it would otherwise leave "keep" with no reopening
    # to restore from.
    op.create_check_constraint(
        "ck_run_questions_reopened_together",
        "run_questions",
        "(reopened_at IS NULL) = (reopened_seq IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_questions_reopened_together", "run_questions")
    op.drop_column("run_questions", "reopened_seq")
    for table, index in INDEXES.items():
        op.drop_index(index, table)
        op.create_index(index, table, ["run_question_id", NUMBERED[table]])
    for table in NUMBERED:
        op.drop_column(table, "seq")
        # Revoking the table right takes the column grants with it; then as it was.
        op.execute(f"REVOKE INSERT ON {table} FROM {APP}")
        op.execute(f"GRANT INSERT ON {table} TO {APP}")
