"""webhook deliveries are numbered from the same sequence as observations

Revision ID: 0016
Revises: 0015

The watcher asks "was this setting looked at after this delivery arrived?". It answered by
comparing two clock times, taken in two processes. A delivery stamped earlier than a look
that was in fact made before it (a clock stepped back, or the API's host a second behind
the watcher's) was marked as answered with no look, and the change it announced waited for
the daily recheck. A delivery now takes its number from the sequence that numbers
observations, so the two can be put in the order they were stored in.
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

SYSTEM = "standingproof_system"


def upgrade() -> None:
    op.add_column("webhook_deliveries", sa.Column("seq", sa.BigInteger(), nullable=True))
    # A delivery already dealt with needs no place in the order. One still waiting is
    # numbered after every look made so far, so it gets a look of its own: the safe side.
    op.execute("UPDATE webhook_deliveries SET seq = 0 WHERE handled_at IS NOT NULL")
    op.execute(
        "UPDATE webhook_deliveries SET seq = nextval(pg_get_serial_sequence('evidence', 'seq')) "
        "WHERE handled_at IS NULL"
    )
    op.execute(
        "ALTER TABLE webhook_deliveries ALTER COLUMN seq "
        "SET DEFAULT nextval(pg_get_serial_sequence('evidence', 'seq'))"
    )
    op.alter_column("webhook_deliveries", "seq", nullable=False)
    # USAGE lets the role that stores deliveries take the next number. It cannot set the
    # sequence back (that needs UPDATE on it).
    op.execute(
        "DO $$ BEGIN EXECUTE format('GRANT USAGE ON SEQUENCE %s TO "
        f"{SYSTEM}', pg_get_serial_sequence('evidence', 'seq')); END $$"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN EXECUTE format('REVOKE USAGE ON SEQUENCE %s FROM "
        f"{SYSTEM}', pg_get_serial_sequence('evidence', 'seq')); END $$"
    )
    op.drop_column("webhook_deliveries", "seq")
