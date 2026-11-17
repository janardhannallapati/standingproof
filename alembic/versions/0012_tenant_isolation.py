"""tenant isolation kept by the database: three roles, and a policy on every table

Revision ID: 0012
Revises: 0011

Until now every query added `WHERE tenant_id = ...` by hand, and the application signed in
as the owner of the tables. One forgotten filter would have shown one tenant another's rows.

From here the running processes sign in as roles that own nothing:

- `standingproof_app` reads and writes customer tables, and on each of them a policy lets
  it see and write only rows whose tenant is the one named in `app.tenant_id` for the
  current transaction. No tenant named: no rows.
- `standingproof_system` finds work across tenants. It has rights on the few tables that
  scheduling needs and none on the rest.
- `standingproof_state` is for the workflow's saved state (schema `workflow`); it gets its
  rights there from `python -m standingproof.workflow setup`, and none here.

Row-level security is FORCED, so it binds the owner too unless the owner is a superuser.
A later migration that must read or change rows as a plain owner switches it off for its
own transaction (`ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`) and back on.

Every new table with a `tenant_id` needs the same three statements; a test fails until it
has them (tests/attacks/test_database_rules.py).
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP = "standingproof_app"
SYSTEM = "standingproof_system"
STATE = "standingproof_state"

# Every table with a tenant_id that the application reads and writes.
TENANT_TABLES = (
    "questionnaires",
    "questionnaire_files",
    "questions",
    "library_answers",
    "policy_docs",
    "policy_chunks",
    "library_answer_embeddings",
    "policy_chunk_embeddings",
    "connections",
    "evidence",
    "runs",
    "run_questions",
    "review_decisions",
    "draft_checks",
    "filled_files",
    "sent_files",
    "answer_evidence",
    "drift_queue",
    "drift_events",
    "answer_reopenings",
)

# What finding work across tenants needs, and no more. Not here: the library, uploaded and
# filled files, questions, decisions, checks of drafts.
SYSTEM_RIGHTS = {
    "tenants": "SELECT, INSERT",
    "connections": "SELECT",
    "answer_evidence": "SELECT",
    "drift_queue": "SELECT",
    "webhook_deliveries": "SELECT, INSERT, UPDATE, DELETE",
}
# On the tables that also hold customer content, rights are given column by column: where
# and when, never what. The system role cannot read an answer, a note or what an
# observation saw, and cannot move a row to another tenant.
SYSTEM_COLUMN_RIGHTS = {
    "evidence": {"SELECT": "id, tenant_id, check_name, scope, status, observed_at, seq"},
    "runs": {"SELECT": "id, tenant_id, status", "UPDATE": "status, finished_at"},
    "run_questions": {
        "SELECT": "id, tenant_id, run_id, status, review, lease_until, enqueue_after",
        # The sweep pushes a due question's next queueing forward. Nothing else: it cannot
        # change a status, a review state or a wording.
        "UPDATE": "enqueue_after",
    },
}

TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    for role in (APP, SYSTEM, STATE):
        # Roles belong to the server, not to this database, so one may already exist.
        op.execute(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
              END IF;
            END $$
            """
        )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP}, {SYSTEM}")

    for table in TENANT_TABLES:
        _secure(table)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP}")
        op.execute(
            f"CREATE POLICY tenant_only ON {table} TO {APP} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )

    # Its own row, to read. A tenant is created by the system role, never by a request.
    _secure("tenants")
    op.execute(f"GRANT SELECT ON tenants TO {APP}")
    op.execute(f"CREATE POLICY tenant_only ON tenants TO {APP} USING (id = {TENANT})")

    # A delivery may be about no tenant at all, so it is the system role's table alone.
    _secure("webhook_deliveries")

    for table, rights in SYSTEM_RIGHTS.items():
        op.execute(f"GRANT {rights} ON {table} TO {SYSTEM}")
        op.execute(
            f"CREATE POLICY across_tenants ON {table} TO {SYSTEM} USING (true) WITH CHECK (true)"
        )
    for table, rights in SYSTEM_COLUMN_RIGHTS.items():
        for command, columns in rights.items():
            op.execute(f"GRANT {command} ({columns}) ON {table} TO {SYSTEM}")
            op.execute(
                f"CREATE POLICY across_tenants_{command.lower()} ON {table} "
                f"FOR {command} TO {SYSTEM} USING (true)"
            )


def downgrade() -> None:
    for table in (*TENANT_TABLES, "tenants", "webhook_deliveries"):
        op.execute(f"DROP POLICY IF EXISTS tenant_only ON {table}")
        for name in ("across_tenants", "across_tenants_select", "across_tenants_update"):
            op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for role in (APP, SYSTEM):
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
    # The roles stay: they belong to the server and another database may use them.
