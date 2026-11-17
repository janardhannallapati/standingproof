"""The audit chain: written with the action it records, and broken visibly by any change."""

import asyncio
import hashlib
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
    # The customer's name is not in the log, plainly or as a hash someone could guess.
    sent = next(row for row in found if row.action == "file.sent")
    assert set(sent.details) == {"run_id", "sent_file_id", "answers"}
    guessable = hashlib.sha256(b"acme corp").hexdigest()
    assert "Acme" not in str([tuple(row) for row in found])
    assert guessable not in str([tuple(row) for row in found])
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

        # The first entry that fails is named. Entries that still hold are counted too,
        # wherever they are: one bad entry does not hide the rest.
        assert problem is not None and problem.seq == where
        assert sound + problem.bad == len(entries(wf))
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


def _as_tenant(db: Db, tenant_id: uuid.UUID, sql: str, **values: Any) -> str | None:
    """Run SQL as the app role for one tenant. Returns the database's refusal, if any."""

    async def go() -> str | None:
        async with db.database.tenant(tenant_id)() as session:
            try:
                await session.execute(text(sql), values)
                await session.commit()
            except DBAPIError as error:
                return str(error.orig)
        return None

    return db.run(go())


FORGED = (
    "INSERT INTO audit_log (tenant_id, seq, at, actor, action, subject, details, prev_hash, hash) "
    "VALUES (:t, :seq, CAST(CAST(:at AS text) AS timestamptz), 'ceo@customer', "
    "'review.approve', 'x', '{}', :prev, :hash)"
)


@pytest.mark.parametrize(
    ("seq", "at", "prev", "why"),
    [
        # A number so high that no honest entry could ever follow it.
        (9223372036854775807, "now", "last", "follow"),
        # A gap, a fork of an entry that exists, and a wrong link.
        (4, "now", "last", "follow"),
        (2, "now", "last", "follow"),
        (3, "now", "0" * 64, "follow"),
        # Backdated, postdated, and a time no program can read back.
        (3, "2020-01-01", "last", "time"),
        (3, "2099-01-01", "last", "time"),
        (3, "infinity", "last", "time"),
    ],
)
def test_the_database_refuses_an_entry_that_does_not_follow_the_last_one_now(
    wf: Db, seq: int, at: str, prev: str, why: str
) -> None:
    owner, _, _ = approved_answer(wf)
    [(last,)] = wf.run(_scalar(wf, "SELECT hash FROM audit_log WHERE seq = 2"))
    values = {"t": owner, "seq": seq, "at": at, "prev": last if prev == "last" else prev}

    refused = _as_tenant(wf, owner, FORGED, hash="a" * 64, **values)

    assert refused is not None and why in refused
    # And honest work goes on.
    note(wf, owner)
    assert checked(wf, owner) == (3, None)


def test_a_well_placed_entry_with_a_made_up_hash_is_found_and_the_rest_is_still_checked(
    wf: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    owner, _, _ = approved_answer(wf, "ours")
    approved_answer(wf, "zzz-later")
    [(last,)] = wf.run(
        _scalar(wf, f"SELECT hash FROM audit_log WHERE seq = 2 AND tenant_id = '{owner}'")  # noqa: S608
    )
    # In the right place, at the right time, linked correctly: only its own hash is a lie.
    assert _as_tenant(wf, owner, FORGED, t=owner, seq=3, at="now", prev=last, hash="a" * 64) is None
    note(wf, owner, 2)

    sound, problem = checked(wf, owner)

    assert problem is not None and (problem.seq, problem.bad) == (3, 1)
    assert sound == 4  # the two before it and the two after it hold
    assert wf.run(audit_command(wf.database, "verify")) == 1
    printed = capsys.readouterr()
    assert "tenant ours: BROKEN at entry 3" in printed.err
    # One tenant's broken chain does not stop the next one being checked.
    assert "tenant zzz-later: 2 entries, chain intact" in printed.out


def test_with_a_key_an_entry_cannot_be_forged_by_someone_who_can_only_run_sql(wf: Db) -> None:
    owner, _, _ = approved_answer(wf)
    [(last,)] = wf.run(_scalar(wf, "SELECT hash FROM audit_log WHERE seq = 2"))
    at = wf.run(_scalar(wf, "SELECT clock_timestamp()"))[0][0]
    # Everything an attacker with SQL knows: the content, the time, the hash before it.
    forged = audit.chain.entry_hash(
        key=None,
        previous=last,
        tenant_id=owner,
        seq=3,
        at=at,
        actor="ceo@customer",
        action="review.approve",
        subject="x",
        details={},
    )
    assert (
        _as_tenant(wf, owner, FORGED, t=owner, seq=3, at=at.isoformat(), prev=last, hash=forged)
        is None
    )

    async def verify_with(key: bytes | None) -> tuple[int, audit.Broken | None]:
        async with wf.sessions() as session:
            return await audit.verify(session, owner, key=key)

    # With no key the forgery passes: the chain then shows that nothing was altered, not
    # who wrote it. (The first two entries were written with no key, so they hold here.)
    assert wf.run(verify_with(None)) == (3, None)
    # With a key the database never sees, the same row does not verify.
    _, problem = wf.run(verify_with(b"k" * 32))
    assert problem is not None


def test_holding_a_lock_as_one_tenant_does_not_block_another_or_the_operator(wf: Db) -> None:
    ours, _, _ = approved_answer(wf, "ours")
    theirs, _, _ = approved_answer(wf, "theirs")

    async def go() -> None:
        async with wf.database.tenant(ours)() as hostile:
            # Every lock a tenant can take by name, and its own chain's row.
            for name in (f"audit:{theirs}", "audit:None", f"audit:{ours}"):
                await hostile.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:name, 0))"), {"name": name}
                )
            await hostile.execute(text("SELECT * FROM audit_heads FOR UPDATE"))
            async with asyncio.timeout(5):
                async with wf.database.tenant(theirs)() as session:
                    await audit.record(
                        session, theirs, audit.Entry(action="test.note", subject="n", details={})
                    )
                    await session.commit()
                async with wf.database.across_tenants() as session:
                    from standingproof.limits import switch

                    await switch.set_stopped(session, None, True, by="priya")

    wf.run(go())

    assert checked(wf, theirs) == (3, None)
    assert checked(wf, None) == (1, None)


def test_approving_three_hundred_at_once_takes_the_chain_once(
    wf: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, _ = approved_answer(wf)
    calls: list[int] = []
    real = review.record

    async def counting(session: Any, tenant_id: Any, *given: Any) -> None:
        calls.append(len(given))
        await real(session, tenant_id, *given)

    monkeypatch.setattr(review, "record", counting)
    owner_sql(
        wf,
        "UPDATE run_questions SET review = 'pending', status = 'ready', version = 1, "
        "final_answer = NULL",
    )

    async def approve() -> int | None:
        async with wf.database.tenant(owner)() as session:
            return await review.approve_ready(
                session, tenant_id=owner, run_id=run_id, count=1, reviewer="asha"
            )

    [(run_id,)] = wf.run(_scalar(wf, "SELECT id FROM runs"))
    assert wf.run(approve()) == 1
    assert calls == [1]  # one call for the batch, however many questions it holds
