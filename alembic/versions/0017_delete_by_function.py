"""the app role deletes through two functions and holds DELETE on no table

Revision ID: 0017
Revises: 0016

A role with DELETE on a table may also LOCK TABLE it in modes that make every other
session wait. After 0015 the app role still held DELETE on three tables, so SQL running as
one tenant could stop every tenant there.

`questions`: the application never deletes a question (a questionnaire that needs its
columns chosen has none), so the right simply goes.

`answer_evidence` and `drift_queue`: rows must go, so each gets a function that removes
them. A SECURITY DEFINER function runs with its owner's rights and a body the caller cannot
change. The owner is a role nobody can sign in as, with DELETE on these two tables and
nothing else. The app role holds EXECUTE only. Row-level security is forced, and inside the
function the policies of the owner apply, so that role has its own policy with the same
tenant rule: a call made as one tenant cannot remove another tenant's row.

Each function fixes its search_path and names its table with the schema, so nothing a caller
creates can be found in place of ours.

The migrating role must be able to hand the functions over: a superuser, or the role that
created `standingproof_deleter` (it then holds the admin option). The role is given CREATE
on the schema only for the hand-over.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

APP = "standingproof_app"
DELETER = "standingproof_deleter"
TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

# function, table, the column it deletes by.
FUNCTIONS = (
    ("forget_answer_evidence", "answer_evidence", "run_question_id"),
    ("drop_from_drift_queue", "drift_queue", "evidence_id"),
)
NO_LONGER_DELETES = ("questions", "answer_evidence", "drift_queue")


def upgrade() -> None:
    # Roles belong to the server, not to this database, so it may already exist.
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DELETER}') THEN
            CREATE ROLE {DELETER} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END $$
        """
    )
    # A role of this name made earlier by someone else is used only if it is what this
    # migration would have made. One that can sign in would hold DELETE, and so a table
    # lock, for whoever has its password; one exempt from row-level security would delete
    # any tenant's row. Nobody signs in as it, so no connection check would ever notice.
    op.execute(
        f"""
        DO $$ BEGIN
          IF EXISTS (
            SELECT FROM pg_roles r WHERE r.rolname = '{DELETER}'
              AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
                   OR r.rolcreatedb OR r.rolreplication
                   OR EXISTS (SELECT FROM pg_auth_members m
                              WHERE (m.roleid = r.oid OR m.member = r.oid)
                                AND (m.set_option OR m.inherit_option)))
          ) THEN
            RAISE EXCEPTION '{DELETER} must be a role nobody can sign in as or act as, '
              'with no privileged attribute and no membership';
          END IF;
        END $$
        """  # noqa: S608 - the name is the constant above
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {DELETER}")
    # To hand a function to another role, the migrating role must be able to act as it,
    # and that role must be able to create in the schema. Both end with this migration.
    op.execute(f"GRANT {DELETER} TO CURRENT_USER WITH SET TRUE, INHERIT FALSE")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DELETER}")

    for function, table, column in FUNCTIONS:
        # DELETE ... WHERE reads the column it filters by, and the policy reads the tenant.
        op.execute(f"GRANT DELETE, SELECT ({column}, tenant_id) ON {table} TO {DELETER}")
        op.execute(
            f"CREATE POLICY deleter_tenant_only ON {table} TO {DELETER} "
            f"USING (tenant_id = {TENANT})"
        )
        # Static SQL and one typed argument: nothing from a caller is ever part of a statement.
        # Postgres plans the body when the function is made, as the migrating role. The
        # policies are forced and migrations run with row_security off, so for an owner that
        # is not a superuser the plan would be refused. Lifted for that one statement.
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE FUNCTION public.{function}(target uuid) RETURNS void
            LANGUAGE sql SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $$ DELETE FROM public.{table} WHERE {column} = target $$
            """  # noqa: S608
        )
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # A new function may be run by anyone. Only the app role may run these.
        op.execute(f"REVOKE EXECUTE ON FUNCTION public.{function}(uuid) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}(uuid) TO {APP}")
        op.execute(f"ALTER FUNCTION public.{function}(uuid) OWNER TO {DELETER}")

    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {DELETER}")
    op.execute(f"REVOKE {DELETER} FROM CURRENT_USER")

    for table in NO_LONGER_DELETES:
        op.execute(f"REVOKE DELETE ON {table} FROM {APP}")


def downgrade() -> None:
    for table in NO_LONGER_DELETES:
        op.execute(f"GRANT DELETE ON {table} TO {APP}")
    for function, table, _ in FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{function}(uuid)")
        op.execute(f"DROP POLICY deleter_tenant_only ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM {DELETER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {DELETER}")
    # The role stays: it belongs to the server and another database may use it.
