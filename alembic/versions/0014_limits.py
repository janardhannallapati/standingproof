"""limits: a stop switch and a daily spend ceiling, per tenant and for the deployment

Revision ID: 0014
Revises: 0013

One row per tenant that has a setting of its own, and one with tenant_id NULL for the
deployment. An operator changes them (`python -m standingproof.limits`), as the system
role. The app role may only read its own tenant's row and the deployment's: a tenant
cannot raise its own ceiling or switch itself back on.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP = "standingproof_app"
SYSTEM = "standingproof_system"
TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "limits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        ),
        sa.Column("stopped", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("daily_limit_usd", sa.Numeric(10, 2)),
        sa.Column("changed_by", sa.String(200), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id", name="uq_limits_tenant", postgresql_nulls_not_distinct=True
        ),
    )
    op.execute("ALTER TABLE limits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE limits FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT ON limits TO {APP}")
    op.execute(
        f"CREATE POLICY own_or_deployment ON limits FOR SELECT TO {APP} "
        f"USING (tenant_id = {TENANT} OR tenant_id IS NULL)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON limits TO {SYSTEM}")
    op.execute(f"CREATE POLICY across_tenants ON limits TO {SYSTEM} USING (true) WITH CHECK (true)")


def downgrade() -> None:
    op.drop_table("limits")
