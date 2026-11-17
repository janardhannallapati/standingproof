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
        wf, wf.sessions, "SELECT policyname, roles FROM pg_policies WHERE tablename = :t", t=table
    )
    policies = [(name, list(roles)) for name, roles in found]

    assert (enabled, forced) == (True, True)
    if table == "webhook_deliveries":
        # A delivery may belong to no tenant: the app role has no rights on it at all.
        assert policies == [("across_tenants", [SYSTEM_ROLE])]
        nobody_special = wf.database.tenant(uuid.uuid4())
        reason = refused(wf, nobody_special, f"SELECT 1 FROM {table}")  # noqa: S608
        assert "permission denied" in reason
    else:
        assert ("tenant_only", [APP_ROLE]) in policies


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
    assert "row-level security" in refused(
        wf, as_us, "UPDATE questionnaires SET tenant_id = :t", t=theirs
    )
    # Their rows are not refused but simply not there: nothing to update, nothing to delete.
    assert rows(wf, as_us, "UPDATE runs SET status = 'cancelled' RETURNING tenant_id") == [(ours,)]
    assert rows(wf, as_us, "DELETE FROM evidence WHERE tenant_id = :t RETURNING id", t=theirs) == []
    assert count(wf, wf.sessions, "evidence", theirs) == 1
    # With no tenant named, an insert is refused too.
    nobody = build_session_factory(wf.database.app_engine)
    assert "row-level security" in refused(wf, nobody, library_answer, t=ours)


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

    assert len(rows(wf, system, "SELECT DISTINCT tenant_id FROM run_questions")) == 2
    assert len(rows(wf, system, "SELECT status, observed_at FROM evidence")) == 2
    for sql in (
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
        assert "permission denied" in refused(wf, system, sql), sql
