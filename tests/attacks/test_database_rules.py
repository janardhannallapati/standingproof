"""What the database itself refuses, whatever the application's code says.

Each test signs in as a role the running processes use and tries what a bug or an
attacker would: a query with no tenant filter, a row written for another tenant, a table
it has no business reading.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.db import APP_ROLE, STATE_ROLE, SYSTEM_ROLE, build_engine, build_session_factory
from standingproof.models import Base
from tests.conftest import Db, settings_for
from tests.drift.rig import approved_answer
from tests.workflow.seed import wf

__all__ = ["wf"]

Sessions = async_sessionmaker[AsyncSession]

# Tables that carry a tenant: found in the models, so a new one is covered without a list.
TENANT_TABLES = sorted(
    table.name for table in Base.metadata.sorted_tables if "tenant_id" in table.columns
)
APP_TABLES = [name for name in TENANT_TABLES if name != "webhook_deliveries"]


def rows(db: Db, sessions: Sessions, sql: str, **values: Any) -> list[Any]:
    async def go() -> list[Any]:
        async with sessions() as session:
            return list((await session.execute(text(sql), values)).all())

    return db.run(go())


def refused(db: Db, sessions: Sessions, sql: str, **values: Any) -> str:
    """Run a statement that must fail, and return the database's reason."""

    async def go() -> str:
        async with sessions() as session:
            with pytest.raises(DBAPIError) as error:
                await session.execute(text(sql), values)
                await session.commit()
            return str(error.value.orig)

    return db.run(go())


def count(db: Db, sessions: Sessions, table: str, tenant_id: uuid.UUID | None = None) -> int:
    where = "" if tenant_id is None else " WHERE tenant_id = :t"
    values = {} if tenant_id is None else {"t": tenant_id}
    # The table names come from our own models, never from input.
    [(found,)] = rows(db, sessions, f"SELECT count(*) FROM {table}{where}", **values)  # noqa: S608
    return int(found)


def owner_sql(db: Db, sql: str) -> None:
    async def go() -> None:
        async with db.sessions() as session:
            await session.execute(text(sql))
            await session.commit()

    db.run(go())


def two_tenants(db: Db) -> tuple[uuid.UUID, uuid.UUID]:
    ours, _, _ = approved_answer(db, "ours")
    theirs, _, _ = approved_answer(db, "theirs")
    return ours, theirs


@pytest.mark.parametrize("role", [APP_ROLE, SYSTEM_ROLE, STATE_ROLE])
def test_no_running_role_is_privileged_or_owns_a_table(wf: Db, role: str) -> None:
    [(unsafe, owned)] = rows(
        wf,
        wf.sessions,
        """
        SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication,
               (SELECT count(*) FROM pg_tables WHERE tableowner = :role)
        FROM pg_roles WHERE rolname = :role
        """,
        role=role,
    )
    assert (unsafe, owned) == (False, 0)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE loot (id int)",
        "ALTER TABLE questionnaires DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE questionnaires NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY tenant_only ON questionnaires",
        "DROP TABLE questionnaires",
        "TRUNCATE questionnaires CASCADE",
        "CREATE ROLE helper SUPERUSER",
        "SET ROLE standingproof_system",
    ],
)
def test_the_app_role_cannot_change_the_rules(wf: Db, statement: str) -> None:
    ours, _ = two_tenants(wf)

    reason = refused(wf, wf.database.tenant(ours), statement)

    assert "permission denied" in reason or "must be owner" in reason


@pytest.mark.parametrize("table", [*TENANT_TABLES, "tenants"])
def test_every_tenant_table_has_a_forced_policy(wf: Db, table: str) -> None:
    [(enabled, forced)] = rows(
        wf,
        wf.sessions,
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(:t)",
        t=table,
    )
    found = rows(
        wf,
        wf.sessions,
        "SELECT policyname, roles, cmd, qual, with_check FROM pg_policies WHERE tablename = :t",
        t=table,
    )
    policies = [(name, list(roles)) for name, roles, *_ in found]

    assert (enabled, forced) == (True, True)
    if table == "webhook_deliveries":
        # A delivery may belong to no tenant: the app role has no rights on it at all.
        assert policies == [("across_tenants", [SYSTEM_ROLE])]
        nobody_special = wf.database.tenant(uuid.uuid4())
        reason = refused(wf, nobody_special, f"SELECT 1 FROM {table}")  # noqa: S608
        assert "permission denied" in reason
        return
    if table == "limits":
        # Read-only for a tenant: its own row and the deployment's. Tested in test_limits.py.
        [(command, using)] = [
            (cmd, qual) for name, _, cmd, qual, _ in found if name == "own_or_deployment"
        ]
        assert command == "SELECT" and "tenant_id IS NULL" in using and "app.tenant_id" in using
        return
    assert ("tenant_only", [APP_ROLE]) in policies
    # The name is not the rule. What the policy says is checked too: it covers every
    # command, and compares the row's tenant with the one the transaction named.
    [(command, using, check)] = [
        (cmd, qual, with_check) for name, _, cmd, qual, with_check in found if name == "tenant_only"
    ]
    column = "id" if table == "tenants" else "tenant_id"
    names_the_tenant = (
        f"({column} = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
    )
    assert names_the_tenant in using
    if table == "tenants":
        assert command == "SELECT"
        return
    assert command == "ALL" and names_the_tenant in check
    # And a row may only point at rows this tenant can see: a foreign key is checked by
    # the database with no regard to policies, so the policy has to ask.
    for key in Base.metadata.tables[table].foreign_keys:
        parent = key.column.table.name
        if parent != "tenants":
            assert f"FROM {parent}" in check and f"{parent}.id = {table}.{key.parent.name}" in check


def test_a_query_with_no_filter_sees_one_tenant_and_none_without_a_tenant(wf: Db) -> None:
    ours, theirs = two_tenants(wf)
    nobody = build_session_factory(wf.database.app_engine)
    in_both = 0

    for table in APP_TABLES:
        mine, other = count(wf, wf.sessions, table, ours), count(wf, wf.sessions, table, theirs)
        # No WHERE at all: what a forgotten filter looks like.
        visible = count(wf, wf.database.tenant(ours), table)
        unnamed = count(wf, nobody, table)
        assert (table, visible, unnamed) == (table, mine, 0)
        in_both += bool(mine and other)

    # Not an empty promise: both tenants really had rows in most of the tables.
    assert in_both >= 7
    assert rows(wf, wf.database.tenant(ours), "SELECT name FROM tenants") == [("ours",)]
    assert rows(wf, nobody, "SELECT name FROM tenants") == []


def test_a_row_cannot_be_written_for_or_moved_to_another_tenant(wf: Db) -> None:
    ours, theirs = two_tenants(wf)
    as_us = wf.database.tenant(ours)
    library_answer = (
        "INSERT INTO library_answers (tenant_id, question, answer, owner, expires_on) "
        "VALUES (:t, 'q', 'a', 'o', '2099-01-01')"
    )

    assert "row-level security" in refused(wf, as_us, library_answer, t=theirs)
    # The tenant of a row cannot be changed at all: there is no right to update that column.
    assert "permission denied" in refused(
        wf, as_us, "UPDATE questionnaires SET tenant_id = :t", t=theirs
    )
    # Their rows are not refused but simply not there: nothing to update, nothing to delete.
    assert rows(wf, as_us, "UPDATE runs SET status = 'cancelled' RETURNING tenant_id") == [(ours,)]
    assert (
        rows(wf, as_us, "DELETE FROM questions WHERE tenant_id = :t RETURNING id", t=theirs) == []
    )
    assert count(wf, wf.sessions, "questions", theirs) == 1
    # And where the application never deletes, it has no right to.
    assert "permission denied" in refused(wf, as_us, "DELETE FROM evidence")
    # Nor to choose where an observation stands in the order: the database numbers it.
    forged = (
        "INSERT INTO evidence (tenant_id, check_name, scope, status, facts, raw_result, "
        "statement, observed_at, seq) OVERRIDING SYSTEM VALUE "
        "VALUES (:t, 'github.branch_protection', 'acme/app', 'observed', '{}', '{}', 's', "
        "now(), 9000000000)"
    )
    assert "permission denied" in refused(wf, as_us, forged, t=ours)
    # Nor to renumber or rewrite what was seen and decided, even for its own tenant.
    for sql in (
        "UPDATE evidence SET seq = DEFAULT",
        "UPDATE evidence SET facts = '{}'",
        "UPDATE review_decisions SET decided_by = 'someone else'",
        "UPDATE sent_files SET customer = 'someone else'",
    ):
        assert "permission denied" in refused(wf, as_us, sql), sql
    # With no tenant named, an insert is refused too.
    nobody = build_session_factory(wf.database.app_engine)
    assert "row-level security" in refused(wf, nobody, library_answer, t=ours)


def test_a_row_cannot_point_at_another_tenants_row(wf: Db) -> None:
    ours, theirs = two_tenants(wf)
    as_us = wf.database.tenant(ours)
    [(their_evidence,)] = rows(
        wf, wf.sessions, "SELECT id FROM evidence WHERE tenant_id = :t", t=theirs
    )
    [(their_item,)] = rows(
        wf, wf.sessions, "SELECT id FROM run_questions WHERE tenant_id = :t", t=theirs
    )

    # Ours by tenant_id, theirs by reference. Left alone, the first would block their own
    # queue row for that observation, and a foreign-key error would say "that id exists".
    queue = "INSERT INTO drift_queue (evidence_id, tenant_id, trigger) VALUES (:e, :t, 'manual')"
    decision = (
        "INSERT INTO review_decisions (tenant_id, run_question_id, decision, decided_by) "
        "VALUES (:t, :i, 'approve', 'us')"
    )
    assert "row-level security" in refused(wf, as_us, queue, e=their_evidence, t=ours)
    assert "row-level security" in refused(wf, as_us, decision, i=their_item, t=ours)
    # The same refusal as for an id nobody has: nothing is learned from it.
    assert "row-level security" in refused(wf, as_us, queue, e=uuid.uuid4(), t=ours)
    assert "row-level security" in refused(
        wf, as_us, "UPDATE answer_evidence SET evidence_id = :e", e=their_evidence
    )


def test_no_reference_can_be_moved_to_another_tenants_row(wf: Db) -> None:
    """Every foreign key between tenant tables, read from the models, tried in turn."""
    ours, theirs = two_tenants(wf)
    as_us = wf.database.tenant(ours)
    tried = 0

    for table in APP_TABLES:
        for key in Base.metadata.tables[table].foreign_keys:
            parent, column = key.column.table.name, key.parent.name
            if parent == "tenants" or not count(wf, wf.sessions, table, ours):
                continue
            # Table and column names come from our own models.
            find = f"SELECT id FROM {parent} WHERE tenant_id = :t LIMIT 1"  # noqa: S608
            move = f"UPDATE {table} SET {column} = :id"  # noqa: S608
            target = rows(wf, wf.sessions, find, t=theirs)
            if not target:
                continue
            reason = refused(wf, as_us, move, id=target[0][0])
            # Refused by the policy, or (on an append-only table) because nothing there
            # may be updated at all.
            expected = "permission denied" if table in APPEND_ONLY else "row-level security"
            assert expected in reason, (table, column)
            tried += 1

    assert tried >= 7


def test_the_tenant_holds_across_commits_and_does_not_stay_on_the_connection(wf: Db) -> None:
    ours, _ = two_tenants(wf)
    names = text("SELECT name FROM tenants")

    async def go() -> tuple[list[Any], list[Any], list[Any]]:
        # One connection, so the second session is certain to get the first one's back.
        engine = build_engine(settings_for(wf.url).role_url("app"), pool_size=1, max_overflow=0)
        try:
            async with build_session_factory(engine, tenant_id=ours)() as session:
                first = (await session.execute(names)).all()
                await session.commit()
                # A new transaction on the same session: still ours.
                second = (await session.execute(names)).all()
            async with build_session_factory(engine)() as session:
                after = (await session.execute(names)).all()
            return list(first), list(second), list(after)
        finally:
            await engine.dispose()

    assert wf.run(go()) == ([("ours",)], [("ours",)], [])


def test_the_system_role_finds_work_but_cannot_read_customer_content(wf: Db) -> None:
    two_tenants(wf)
    system = wf.database.across_tenants

    ours, theirs = (tenant_id for (tenant_id,) in rows(wf, wf.sessions, "SELECT id FROM tenants"))
    assert len(rows(wf, system, "SELECT DISTINCT tenant_id FROM run_questions")) == 2
    assert len(rows(wf, system, "SELECT status, observed_at FROM evidence")) == 2
    assert len(rows(wf, system, "UPDATE run_questions SET enqueue_after = now() RETURNING id")) == 2

    def finished_by_system() -> int:
        return len(rows(wf, system, "UPDATE runs SET status = 'finished' RETURNING id"))

    # It may close a running run that has nothing left to do, and no other run.
    owner_sql(wf, "UPDATE runs SET status = 'cancelled'")
    assert finished_by_system() == 0
    owner_sql(wf, "UPDATE runs SET status = 'running'")
    owner_sql(wf, "UPDATE run_questions SET status = 'queued'")
    assert finished_by_system() == 0
    owner_sql(wf, "UPDATE run_questions SET status = 'ready'")
    assert finished_by_system() == 2
    for sql in (
        # Where and when, never what: not an answer, a note, a citation or a cost.
        "SELECT answer FROM run_questions",
        "SELECT final_answer FROM run_questions",
        "SELECT note, citations FROM run_questions",
        "SELECT * FROM run_questions",
        "SELECT max_cost_usd FROM runs",
        "UPDATE run_questions SET answer = 'planted'",
        "UPDATE run_questions SET final_answer = 'planted', review = 'approved'",
        "UPDATE run_questions SET status = 'ready'",
        "UPDATE run_questions SET review = 'approved'",
        "SELECT version, reviewed_at FROM run_questions",
        f"UPDATE run_questions SET tenant_id = '{theirs}' WHERE tenant_id = '{ours}'",  # noqa: S608
        f"UPDATE runs SET tenant_id = '{theirs}'",  # noqa: S608
        "INSERT INTO run_questions (id) VALUES (gen_random_uuid())",
        "DELETE FROM runs",
        # It may close a run that has nothing left to do, and that is all it may set.
        "UPDATE runs SET status = 'running'",
        "UPDATE runs SET status = 'cancelled'",
        "SELECT facts FROM evidence",
        "SELECT statement FROM evidence",
        "SELECT answer FROM library_answers",
        "SELECT content FROM questionnaire_files",
        "SELECT text FROM questions",
        "SELECT final_answer FROM review_decisions",
        "SELECT content FROM filled_files",
        "DELETE FROM run_questions",
        "UPDATE evidence SET status = 'observed'",
    ):
        reason = refused(wf, system, sql)
        assert "permission denied" in reason or "row-level security" in reason, sql


# The three tables the application deletes rows from. On these it holds DELETE, which also
# permits a table lock; see SECURITY.md for what that leaves open.
MAY_DELETE = {"answer_evidence", "drift_queue", "questions"}
LOCK_MODES = (
    "SHARE UPDATE EXCLUSIVE",
    "SHARE",
    "SHARE ROW EXCLUSIVE",
    "EXCLUSIVE",
    "ACCESS EXCLUSIVE",
)
# Written once and never changed. The app role may not update any column of these.
APPEND_ONLY = {
    "evidence",
    "review_decisions",
    "draft_checks",
    "filled_files",
    "sent_files",
    "drift_events",
    "answer_reopenings",
}


@pytest.mark.parametrize("table", [t for t in APP_TABLES if t not in MAY_DELETE])
def test_a_tenant_cannot_lock_a_whole_table_and_make_every_tenant_wait(wf: Db, table: str) -> None:
    ours, _ = two_tenants(wf)

    for mode in LOCK_MODES:
        reason = refused(wf, wf.database.tenant(ours), f"LOCK TABLE {table} IN {mode} MODE")
        assert "permission denied" in reason, (table, mode)


@pytest.mark.parametrize("table", APP_TABLES)
def test_the_app_role_updates_columns_never_a_whole_table_and_never_the_tenant(
    wf: Db, table: str
) -> None:
    columns = [column.name for column in Base.metadata.tables[table].columns]
    [(whole, deletes)] = rows(
        wf,
        wf.sessions,
        "SELECT has_table_privilege(:role, :t, 'UPDATE'), has_table_privilege(:role, :t, 'DELETE')",
        role=APP_ROLE,
        t=table,
    )
    may_update = {
        name
        for name in columns
        if rows(
            wf,
            wf.sessions,
            "SELECT has_column_privilege(:role, :t, :c, 'UPDATE')",
            role=APP_ROLE,
            t=table,
            c=name,
        )[0][0]
    }

    assert whole is False
    assert deletes is (table in MAY_DELETE)
    if table in ("audit_log", "limits") or table in APPEND_ONLY:
        # Append-only, and read-only: nothing here is the app's to change.
        assert may_update == set()
    elif table == "audit_heads":
        assert may_update == {"id"}
    else:
        # Every column a migration added, so that a new one is not forgotten. Never the
        # tenant: a row does not change hands.
        assert may_update == set(columns) - {"tenant_id"}
