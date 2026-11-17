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
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION audit_log_is_append_only()")
