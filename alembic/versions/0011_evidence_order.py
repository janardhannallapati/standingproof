"""evidence: the order observations were stored in, kept by the database

Revision ID: 0011
Revises: 0010

"Which look is the newest?" was answered by `observed_at`, the application's clock. A clock
that is stepped back (it happens: time sync, a virtual machine resuming) gives a later look
an earlier time, and a change was then missed. `seq` comes from a database sequence, which
never goes backwards. `observed_at` stays as what it is: the time to show a person.
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidence", sa.Column("seq", sa.BigInteger(), nullable=True))
    # Existing rows keep the order they had until now.
    op.execute(
        """
        UPDATE evidence SET seq = ordered.n
        FROM (SELECT id, row_number() OVER (ORDER BY observed_at, id) AS n FROM evidence) ordered
        WHERE evidence.id = ordered.id
        """
    )
    op.alter_column("evidence", "seq", nullable=False)
    # ALWAYS: the application cannot choose a row's place in the order.
    op.execute("ALTER TABLE evidence ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY")
    op.execute(
        "SELECT setval(pg_get_serial_sequence('evidence', 'seq'), "
        "(SELECT coalesce(max(seq), 0) + 1 FROM evidence), false)"
    )
    op.drop_index("ix_evidence_lookup", "evidence")
    op.create_index("ix_evidence_lookup", "evidence", ["tenant_id", "check_name", "scope", "seq"])


def downgrade() -> None:
    op.drop_index("ix_evidence_lookup", "evidence")
    op.create_index(
        "ix_evidence_lookup", "evidence", ["tenant_id", "check_name", "scope", "observed_at"]
    )
    op.drop_column("evidence", "seq")
