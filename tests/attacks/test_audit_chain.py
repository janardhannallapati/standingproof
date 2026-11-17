"""The audit chain: written with the action it records, and broken visibly by any change."""

import asyncio
import io
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from standingproof import audit
from standingproof.audit.__main__ import run as audit_command
from standingproof.workflow import review
from tests.conftest import Db
from tests.drift.rig import OPEN, approved_answer, examine_all, observe
from tests.test_checks_api import connect
from tests.test_files_api import decided_run
from tests.workflow.seed import wf

__all__ = ["wf"]


def entries(db: Db, where: str = "", **values: Any) -> list[Any]:
    """Read as the owner: every chain, in order."""

    async def go() -> list[Any]:
        async with db.sessions() as session:
            # `where` is a constant of this file.
            sql = (
                "SELECT tenant_id, seq, actor, action, subject, details FROM audit_log "  # noqa: S608
                f"{where} ORDER BY tenant_id, seq"
            )
            rows = await session.execute(text(sql), values)
            return list(rows.all())

    return db.run(go())


def owner_sql(db: Db, *statements: str) -> None:
    async def go() -> None:
        async with db.sessions() as session:
            for statement in statements:
                await session.execute(text(statement))
            await session.commit()

    db.run(go())


def checked(db: Db, tenant_id: uuid.UUID | None) -> tuple[int, audit.Broken | None]:
    async def go() -> tuple[int, audit.Broken | None]:
        async with db.sessions() as session:
            return await audit.verify(session, tenant_id)

    return db.run(go())


def note(db: Db, tenant_id: uuid.UUID, count: int = 1) -> None:
    """Append plain entries as the tenant, the way the application does."""

    async def one(number: int) -> None:
        async with db.database.tenant(tenant_id, actor="asha")() as session:
            await audit.record(
                session,
                tenant_id,
                audit.Entry(action="test.note", subject=f"n:{number}", details={}),
            )
            await session.commit()

    async def go() -> None:
        await asyncio.gather(*(one(number) for number in range(count)))

    db.run(go())


def test_work_on_a_run_leaves_one_entry_per_decision_in_the_tenants_chain(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)

    found = entries(wf)

    assert [(row.seq, row.actor, row.action) for row in found] == [
        (1, "system", "run.start"),
        (2, "asha", "review.approve"),
        (3, "watcher", "answer.reopen"),
    ]
    assert {row.tenant_id for row in found} == {owner}
    assert found[1].subject == f"run_question:{item}"
    # The hash of the approved wording, never the wording.
    assert len(found[1].details["wording_sha256"]) == 64
    assert "Yes, two reviews." not in str([tuple(row) for row in found])
    assert checked(wf, owner) == (3, None)


def test_requests_are_recorded_under_the_person_who_made_them(api: TestClient, db: Db) -> None:
    assert connect(api).status_code == 201
    assert connect(api).status_code == 200  # already connected: nothing happened, nothing recorded
    answer = {"question": "MFA?", "answer": "Yes.", "owner": "it", "expires_on": "2099-01-01"}
    assert api.post("/library/answers", json=answer).status_code == 201
    policy = io.BytesIO(b"# Access\n\nAll staff use MFA.\n")
    files = {"file": ("access.md", policy, "text/markdown")}
    assert api.post("/library/policies", files=files, data={"title": "Access"}).status_code == 201
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    api.post(f"/runs/{run_id}/file").json()  # asked again: the same file, no second entry
    api.post(f"/runs/{run_id}/file/{file_id}/sent", json={"customer": "Acme Corp"})
    api.post(f"/runs/{run_id}/cancel")  # already finished: nothing to cancel

    found = entries(db)

    assert [row.action for row in found if row.actor == "dev-reviewer"] == [
        "connection.add",
        "library.answer.add",
        "library.policy.add",
        "run.start",
        "review.approve",
        "review.edit",
        "review.reject",
        "file.make",
        "file.sent",
    ]
    assert [row.seq for row in found] == list(range(1, len(found) + 1))
    assert checked(db, found[0].tenant_id) == (len(found), None)


def test_a_decision_and_its_entry_are_stored_together_or_not_at_all(
    wf: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, item, _ = approved_answer(wf)
    before = entries(wf)

    async def fails(*_: Any, **__: Any) -> None:
        raise RuntimeError("the audit log could not be written")

    monkeypatch.setattr(review, "record", fails)

    async def reject() -> None:
        async with wf.database.tenant(owner)() as session:
            await review.decide(
                session,
                tenant_id=owner,
                item_id=item,
                decision="reject",
                version=1,
                reviewer="asha",
                max_redrafts=2,
                max_chars=4000,
                comment="no",
            )

    # The question was approved by the rig, so first put it back in front of a person.
    owner_sql(wf, "UPDATE run_questions SET review = 'pending', version = 1")
    with pytest.raises(RuntimeError, match="audit log"):
        wf.run(reject())

    [(state,)] = wf.run(_scalar(wf, "SELECT review FROM run_questions"))
    assert state == "pending"
    assert entries(wf) == before


def _scalar(db: Db, sql: str) -> Any:
    async def go() -> list[Any]:
        async with db.sessions() as session:
            return list((await session.execute(text(sql))).all())

    return go()


TAMPER = {
    "changed": ("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2", 2, "content"),
    "detail changed": (
        """UPDATE audit_log SET details = '{"batch": true}' WHERE seq = 2""",
        2,
        "content",
    ),
    "removed": ("DELETE FROM audit_log WHERE seq = 2", 2, "missing"),
    "first removed": ("DELETE FROM audit_log WHERE seq = 1", 1, "missing"),
    "swapped": (
        "UPDATE audit_log SET seq = CASE seq WHEN 2 THEN 3 WHEN 3 THEN 2 ELSE seq END",
        2,
        "does not follow",
    ),
    "rewritten with its own hash": (
        "UPDATE audit_log SET action = 'review.reject', hash = repeat('a', 64) WHERE seq = 2",
        2,
        "content",
    ),
}


@pytest.mark.parametrize("how", TAMPER)
def test_verify_names_the_first_entry_that_was_tampered_with(
    wf: Db, how: str, capsys: pytest.CaptureFixture[str]
) -> None:
    owner, _, _ = approved_answer(wf, "ours")
    note(wf, owner, 3)
    statement, where, why = TAMPER[how]
    # What it takes to tamper at all: the owner's login, and switching the trigger off.
    owner_sql(
        wf,
        "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change",
        "ALTER TABLE audit_log DROP CONSTRAINT uq_audit_log_seq",
        statement,
        "ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change",
    )
    try:
        sound, problem = checked(wf, owner)

        assert problem is not None and (sound, problem.seq) == (where - 1, where)
        assert why in problem.reason
        # The command says so, and fails.
        assert wf.run(audit_command(wf.database, "verify")) == 1
        assert f"tenant ours: BROKEN at entry {where}" in capsys.readouterr().err
    finally:
        owner_sql(
            wf,
            "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change",
            "DELETE FROM audit_log",
            "ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change",
            "ALTER TABLE audit_log ADD CONSTRAINT uq_audit_log_seq "
            "UNIQUE NULLS NOT DISTINCT (tenant_id, seq)",
        )


def test_an_untouched_chain_verifies_and_the_command_prints_its_head(
    wf: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    owner, _, _ = approved_answer(wf, "ours")

    assert wf.run(audit_command(wf.database, "verify")) == 0
    assert "tenant ours: 2 entries, chain intact" in capsys.readouterr().out
    assert wf.run(audit_command(wf.database, "head")) == 0
    [(digest,)] = wf.run(_scalar(wf, "SELECT hash FROM audit_log WHERE seq = 2"))
    assert f"tenant ours: entry 2, hash {digest}" in capsys.readouterr().out


def test_nobody_can_change_or_remove_an_entry(wf: Db) -> None:
    owner, _, _ = approved_answer(wf)

    async def attempt(sessions: Any, sql: str) -> str:
        async with sessions() as session:
            with pytest.raises(DBAPIError) as refused:
                await session.execute(text(sql))
            return str(refused.value.orig)

    for sql in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"):
        # The roles the application runs as have no right to; the owner is stopped by a trigger.
        assert "permission denied" in wf.run(attempt(wf.database.tenant(owner), sql))
        assert "permission denied" in wf.run(attempt(wf.database.across_tenants, sql))
        assert "append-only" in wf.run(attempt(wf.sessions, sql))
    assert "permission denied" in wf.run(attempt(wf.database.tenant(owner), "TRUNCATE audit_log"))
    assert checked(wf, owner) == (2, None)


def test_a_tenant_reads_and_extends_only_its_own_chain(wf: Db) -> None:
    ours, _, _ = approved_answer(wf, "ours")
    theirs, _, _ = approved_answer(wf, "theirs")

    async def go() -> tuple[int, int, str, str]:
        async with wf.database.tenant(ours)() as session:
            mine = len((await session.execute(text("SELECT 1 FROM audit_log"))).all())
            with pytest.raises(DBAPIError) as planted:
                await audit.record(
                    session, theirs, audit.Entry(action="x", subject="y", details={})
                )
        async with wf.database.across_tenants() as session:
            system = len((await session.execute(text("SELECT 1 FROM audit_log"))).all())
            with pytest.raises(DBAPIError) as crossed:
                await audit.record(session, ours, audit.Entry(action="x", subject="y", details={}))
        return mine, system, str(planted.value.orig), str(crossed.value.orig)

    mine, system, planted, crossed = wf.run(go())

    # The system role sees the deployment's chain only, which is empty here.
    assert (mine, system) == (2, 0)
    assert "row-level security" in planted and "row-level security" in crossed
    assert checked(wf, theirs) == (2, None)


def test_twenty_writers_at_once_make_one_unbroken_chain(wf: Db) -> None:
    owner, _, _ = approved_answer(wf)

    note(wf, owner, 20)

    assert checked(wf, owner) == (22, None)


def test_details_that_would_not_read_back_the_same_are_refused(wf: Db) -> None:
    owner, _, _ = approved_answer(wf)

    async def go() -> None:
        async with wf.database.tenant(owner)() as session:
            await audit.record(
                session, owner, audit.Entry(action="x", subject="y", details={"cost": 0.1})
            )

    with pytest.raises(TypeError, match="float"):
        wf.run(go())
