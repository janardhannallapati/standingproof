"""audit_log: a hash-chained, append-only record of decisions

Revision ID: 0013
Revises: 0012

One chain per tenant, and one for the deployment (tenant_id NULL). Append-only twice over:
the running roles are granted SELECT and INSERT and nothing else, and a trigger refuses
UPDATE and DELETE from anyone, the owner included. (An owner can still drop the trigger;
see standingproof/audit/chain.py for what the chain does and does not prove.)
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

APP = "standingproof_app"
SYSTEM = "standingproof_system"
TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # No ON DELETE CASCADE: a tenant with a record cannot be deleted from under it.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id")),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "seq", name="uq_audit_log_seq", postgresql_nulls_not_distinct=True
        ),
    )
    op.execute(
        """
        CREATE FUNCTION audit_log_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit_log is append-only';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only()"
    )

    # The database itself checks that an entry is the next one, now. Without this, whoever
    # could run SQL as a tenant could add an entry far ahead (and no honest entry could
    # ever follow), fork or skip a number, or date one in another year. What it cannot
    # check is the hash, whose spelling is the application's: `verify` does that.
    op.execute(
        """
        CREATE FUNCTION audit_log_must_follow() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          last_seq bigint := 0;
          last_hash text := repeat('0', 64);
        BEGIN
          SELECT seq, hash INTO last_seq, last_hash FROM audit_log
          WHERE tenant_id IS NOT DISTINCT FROM NEW.tenant_id ORDER BY seq DESC LIMIT 1;
          IF NOT FOUND THEN
            last_seq := 0; last_hash := repeat('0', 64);
          END IF;
          IF NEW.seq <> last_seq + 1 OR NEW.prev_hash <> last_hash THEN
            RAISE EXCEPTION 'audit_log: an entry must follow the last one of its chain';
          END IF;
          -- Near the database's clock. Not "after the entry before it": a clock is stepped
          -- back now and then, and an honest entry must not be refused for that. The
          -- order of entries is `seq`, never the time.
          IF NEW.at NOT BETWEEN clock_timestamp() - interval '1 minute'
                            AND clock_timestamp() + interval '1 minute' THEN
            RAISE EXCEPTION 'audit_log: the time of an entry must be now';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_follows BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_must_follow()"
    )

    # One row per chain, there to be locked by whoever writes next (see audit/chain.py).
    op.create_table(
        "audit_heads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id")),
        sa.UniqueConstraint(
            "tenant_id", name="uq_audit_heads_tenant", postgresql_nulls_not_distinct=True
        ),
    )
    op.execute("ALTER TABLE audit_heads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_heads FORCE ROW LEVEL SECURITY")
    # UPDATE on a column is what lets a role lock a row (SELECT ... FOR UPDATE). On one
    # column only: UPDATE on the whole table would also allow LOCK TABLE, and one tenant
    # could then hold every chain at once.
    op.execute(f"GRANT SELECT, INSERT ON audit_heads TO {APP}, {SYSTEM}")
    op.execute(f"GRANT UPDATE (id) ON audit_heads TO {APP}, {SYSTEM}")
    op.execute(
        f"CREATE POLICY tenant_only ON audit_heads TO {APP} "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    op.execute(
        f"CREATE POLICY deployment_only ON audit_heads TO {SYSTEM} "
        "USING (tenant_id IS NULL) WITH CHECK (tenant_id IS NULL)"
    )

    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    # A tenant reads and extends its own chain.
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {APP}")
    op.execute(
        f"CREATE POLICY tenant_only ON audit_log TO {APP} "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    # The system role reads and extends the deployment's chain, and no tenant's.
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {SYSTEM}")
    op.execute(
        f"CREATE POLICY deployment_only ON audit_log TO {SYSTEM} "
        "USING (tenant_id IS NULL) WITH CHECK (tenant_id IS NULL)"
    )


def downgrade() -> None:
    op.drop_table("audit_heads")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION audit_log_is_append_only()")
    op.execute("DROP FUNCTION audit_log_must_follow()")
