"""Looking at a setting: what is queued for comparison, and which settings are due."""

import datetime as dt
import uuid

from fastapi.testclient import TestClient

from standingproof.checks.service import run_check
from standingproof.config import Settings
from standingproof.drift.watch import Setting, due
from standingproof.models import Connection, DriftQueue, Evidence
from tests.conftest import ClientFactory, Db
from tests.drift.rig import CHECK, SCOPE, all_rows, answers, observe, queued
from tests.test_checks_api import ORG_2FA, backends, connect, run
from tests.workflow.seed import tenant, wf
from tests.workflow.test_review import decide

__all__ = ["wf"]

SETTINGS = Settings(_env_file=None, github_orgs="acme")  # type: ignore[call-arg]
DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)


def test_an_observation_is_queued_with_why_it_was_made(make_api: ClientFactory, db: Db) -> None:
    api = make_api(backends=backends(ORG_2FA))
    connect(api)

    evidence = run(api, "github.org_two_factor", "acme").json()

    [row] = all_rows(db, DriftQueue)
    assert (str(row.evidence_id), row.trigger) == (evidence["id"], "manual")


def test_a_look_that_saw_nothing_is_not_queued(api: TestClient, db: Db) -> None:
    connect(api)

    assert run(api, "github.org_two_factor", "acme").json()["status"] == "not_configured"
    assert queued(db) == 0


def look(db: Db, owner: uuid.UUID, **more: object) -> Evidence:
    async def go() -> Evidence:
        async with db.sessions() as session:
            return await run_check(
                session,
                tenant_id=owner,
                name="github.org_two_factor",
                scope="acme",
                backends=backends(ORG_2FA),
                settings=SETTINGS,
                **more,  # type: ignore[arg-type]
            )

    return db.run(go())


def test_a_recent_observation_is_reused_unless_a_fresh_look_is_asked_for(wf: Db) -> None:
    owner = tenant(wf)

    async def connected() -> None:
        async with wf.sessions() as session:
            session.add(Connection(tenant_id=owner, system="github", scope="acme"))
            await session.commit()

    wf.run(connected())

    first = look(wf, owner)
    again = look(wf, owner)
    fresh = look(wf, owner, max_age=dt.timedelta(0), trigger="webhook")

    assert again.id == first.id
    assert fresh.id != first.id
    assert sorted(row.trigger for row in all_rows(wf, DriftQueue)) == ["run", "webhook"]


def due_now(db: Db, now: dt.datetime) -> list[Setting]:
    async def go() -> list[Setting]:
        async with db.sessions() as session:
            return await due(session, now=now, recheck=DAY, retry=HOUR, limit=10)

    return db.run(go())


def test_a_setting_an_answer_rests_on_is_due_a_day_after_it_was_last_seen(wf: Db) -> None:
    seen = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    owner = tenant(wf)
    answers(wf, owner, observe(wf, owner, at=seen, queue=False))
    # Nobody rests on this one, so nobody looks at it again.
    observe(wf, owner, at=seen, scope="acme/unused", queue=False)

    assert due_now(wf, seen + DAY - HOUR) == []
    assert due_now(wf, seen + DAY) == [Setting(owner, CHECK, SCOPE)]

    # Looked at again: not due until a day after that.
    observe(wf, owner, at=seen + DAY, queue=False)
    assert due_now(wf, seen + DAY + HOUR) == []


def test_after_a_look_that_failed_it_is_tried_again_in_an_hour(wf: Db) -> None:
    seen = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    owner = tenant(wf)
    answers(wf, owner, observe(wf, owner, at=seen, queue=False))
    observe(wf, owner, status="unavailable", at=seen + DAY)

    assert due_now(wf, seen + DAY + dt.timedelta(minutes=59)) == []
    assert due_now(wf, seen + DAY + HOUR) == [Setting(owner, CHECK, SCOPE)]


def test_a_setting_only_rejected_answers_rest_on_is_never_due(wf: Db) -> None:
    seen = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    owner = tenant(wf)
    _, [item] = answers(wf, owner, observe(wf, owner, at=seen, queue=False))
    decide(wf, owner, item, "reject")

    assert due_now(wf, seen + 30 * DAY) == []


def test_the_longest_waiting_setting_comes_first(wf: Db) -> None:
    seen = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    owner = tenant(wf)
    answers(wf, owner, observe(wf, owner, at=seen + HOUR, queue=False))
    answers(wf, owner, observe(wf, owner, at=seen, scope="acme/older", queue=False))

    assert [s.scope for s in due_now(wf, seen + 3 * DAY)] == ["acme/older", SCOPE]
