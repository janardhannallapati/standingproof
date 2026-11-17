"""a reopening is recorded once per wait, not once per change

Revision ID: 0019
Revises: 0018

`answer_reopenings` allowed one row for a change and an answer. That is one too few when a
decision closes a wait and the same change reopens the answer again: a redraft comes to
rest on an older observation, and the newest reading is compared with it. The second wait
then had no record of its own, and the reviewer was shown a question marked reopened with
nothing that said what had changed, and no way to keep it.

The unique constraint goes; an index on the same columns stays for the Changes page. The
application writes a second row only when a new wait begins (`drift/examine.py`).
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_answer_reopenings_once", "answer_reopenings")
    op.create_index(
        "ix_answer_reopenings_event", "answer_reopenings", ["drift_event_id", "run_question_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_answer_reopenings_event", "answer_reopenings")
    # The old rule allows one row for a change and an answer: the first is kept. The
    # policies are forced, so they bind an owner that is not a superuser; lifted for the
    # statement and put back.
    op.execute("ALTER TABLE answer_reopenings NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DELETE FROM answer_reopenings later USING answer_reopenings first
        WHERE later.drift_event_id = first.drift_event_id
          AND later.run_question_id = first.run_question_id
          AND later.seq > first.seq
        """
    )
    op.execute("ALTER TABLE answer_reopenings FORCE ROW LEVEL SECURITY")
    op.create_unique_constraint(
        "uq_answer_reopenings_once", "answer_reopenings", ["drift_event_id", "run_question_id"]
    )
