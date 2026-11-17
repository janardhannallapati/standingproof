"""narrower rights: no table lock for the app role, and no emptying of the audit log

Revision ID: 0015
Revises: 0014

Two things the earlier migrations left open.

A role with UPDATE or DELETE on a whole table may also LOCK TABLE it, in modes that make
every other session wait. SQL running as one tenant could so stop every tenant. UPDATE on
columns allows a row lock and not a table lock, so the app role now holds UPDATE column by
column (and never on tenant_id: a row does not change hands), and DELETE only on the three
tables it deletes rows from. Every new column of a tenant table needs
`GRANT UPDATE (column) ... TO standingproof_app`; a test fails until it has it
(tests/attacks/test_database_rules.py).

TRUNCATE fires no row trigger, so the audit log's "no change" trigger did not cover it. A
statement that emptied `tenants` with CASCADE would have taken the log with it, and an
emptied chain verifies as sound. A statement trigger now refuses it.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

APP = "standingproof_app"

# Where the application's own code deletes rows: a questionnaire's questions when its
# columns are chosen again, what a redrafted answer no longer rests on, and an observation
# once it has been compared.
MAY_DELETE = ("questions", "answer_evidence", "drift_queue")
# Rows here are written once and never changed: what was seen, what was decided, what was
# made and sent. The app role gets no UPDATE on them at all, not even column by column. With
# it, SQL running as a tenant could renumber its own observations (`seq`) or rewrite its own
# record of decisions.
APPEND_ONLY = (
    "evidence",
    "review_decisions",
    "draft_checks",
    "filled_files",
    "sent_files",
    "drift_events",
    "answer_reopenings",
)
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


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP}")
        if table in APPEND_ONLY:
            continue
        # Column by column, as the columns are today. format() with %I quotes each name.
        # The table names are the constants above.
        op.execute(
            f"""
            DO $$
            DECLARE columns text;
            BEGIN
              SELECT string_agg(format('%I', column_name), ', ') INTO columns
              FROM information_schema.columns
              WHERE table_schema = 'public' AND table_name = '{table}'
                AND column_name <> 'tenant_id';
              EXECUTE format('GRANT UPDATE (%s) ON {table} TO {APP}', columns);
            END $$
            """  # noqa: S608
        )
    for table in MAY_DELETE:
        op.execute(f"GRANT DELETE ON {table} TO {APP}")

    # An observation's place in the order is the database's to give. INSERT on the whole
    # table would let a statement name `seq` itself (OVERRIDING SYSTEM VALUE), and drift
    # picks the newest look by that number. INSERT column by column, without `seq`.
    op.execute(f"REVOKE INSERT ON evidence FROM {APP}")
    op.execute(
        """
        DO $$
        DECLARE columns text;
        BEGIN
          SELECT string_agg(format('%I', column_name), ', ') INTO columns
          FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name = 'evidence' AND column_name <> 'seq';
          EXECUTE format('GRANT INSERT (%s) ON evidence TO standingproof_app', columns);
        END $$
        """
    )

    for table in ("audit_log", "audit_heads"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only()"
        )


def downgrade() -> None:
    for table in ("audit_log", "audit_heads"):
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
    op.execute(f"GRANT INSERT ON evidence TO {APP}")
    for table in TENANT_TABLES:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP}")
