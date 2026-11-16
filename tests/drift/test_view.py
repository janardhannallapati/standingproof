"""What the screen is told about a change, function by function."""

import datetime as dt
import hashlib
import uuid

from standingproof.drift import view
from standingproof.models import FilledFile, RunQuestion, SentFile
from tests.conftest import Db
from tests.drift.rig import (
    CHECK,
    OPEN,
    PROTECTED,
    SCOPE,
    answers,
    approved_answer,
    examine_all,
    item_row,
    observe,
)
from tests.workflow.seed import tenant, wf
from tests.workflow.test_review import decide

__all__ = ["wf"]


def reopening(db: Db, item: uuid.UUID) -> view.Reopening | None:
    async def go() -> view.Reopening | None:
        async with db.sessions() as session:
            row = await session.get(RunQuestion, item)
            assert row is not None
            return await view.reopening_of(session, row)

    return db.run(go())


def changes(db: Db, owner: uuid.UUID, limit: int = 50) -> list[view.ChangeEntry]:
    async def go() -> list[view.ChangeEntry]:
        async with db.sessions() as session:
            return await view.changes_page(session, owner, limit=limit)

    return db.run(go())


def watched(db: Db, owner: uuid.UUID) -> list[view.Watched]:
    async def go() -> list[view.Watched]:
        async with db.sessions() as session:
            return await view.watched(session, owner)

    return db.run(go())


def record_sent(db: Db, item: uuid.UUID, customer: str, wording: str) -> None:
    """A sent record for this question with that wording, written as `record_sent` would."""

    async def go() -> None:
        async with db.sessions() as session:
            row = await session.get(RunQuestion, item)
            assert row is not None
            file = FilledFile(
                tenant_id=row.tenant_id,
                run_id=row.run_id,
                key=uuid.uuid4().hex + uuid.uuid4().hex,
                content=b"",
                size_bytes=0,
                sha256="0" * 64,
                written=[],
                left_empty=[],
                writer_version=2,
                created_by="asha",
            )
            session.add(file)
            await session.flush()
            session.add(
                SentFile(
                    tenant_id=row.tenant_id,
                    run_id=row.run_id,
                    filled_file_id=file.id,
                    customer=customer,
                    sent_by="asha",
                    answers=[
                        {
                            "question_id": str(row.question_id),
                            "position": row.position,
                            "wording_sha256": hashlib.sha256(wording.encode()).hexdigest(),
                        }
                    ],
                )
            )
            await session.commit()

    db.run(go())


def test_a_question_that_was_not_reopened_has_no_reopening(wf: Db) -> None:
    _, item, _ = approved_answer(wf)

    assert reopening(wf, item) is None


def test_only_the_setting_that_changed_is_shown_when_an_answer_rests_on_two(wf: Db) -> None:
    owner = tenant(wf)
    protection = observe(wf, owner, queue=False)
    scanning = observe(
        wf, owner, {"secret_scanning_enabled": True}, check="github.secret_scanning", queue=False
    )
    _, [item] = answers(wf, owner, protection)
    wf.run(_also_cites(wf, owner, item, [protection, scanning]))
    decide(wf, owner, item, "approve")
    observe(wf, owner, OPEN)
    examine_all(wf)

    found = reopening(wf, item)

    assert found is not None
    assert [(s.check_name, s.scope) for s in found.settings] == [(CHECK, SCOPE)]
    assert found.was_review == "approved"
    assert [c.fact for c in found.settings[0].changes] == [
        "protected",
        "required_approving_reviews",
    ]


async def _also_cites(db: Db, owner: uuid.UUID, item: uuid.UUID, ids: list[uuid.UUID]) -> None:
    from standingproof.drift.watch import rest_on

    async with db.sessions() as session:
        await rest_on(session, owner, item, [f"evidence:{each}" for each in ids])
        await session.commit()


def test_a_change_and_a_change_back_is_one_setting_with_nothing_different(wf: Db) -> None:
    owner, item, first = approved_answer(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)
    back = observe(wf, owner, dict(PROTECTED))
    examine_all(wf)

    found = reopening(wf, item)

    assert found is not None
    [setting] = found.settings
    # From what the answer first rested on, to the newest reading: the same facts.
    assert setting.changes == []
    assert setting.was is not None
    assert setting.was.statement != "" and setting.now.observed_at > setting.was.observed_at
    assert found.was_review == "approved"
    assert first != back


def test_only_customers_who_have_the_approved_wording_are_named(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    record_sent(wf, item, "Acme Corp", "Yes, two reviews.")
    record_sent(wf, item, "Earlier Ltd", "An older wording that was later replaced.")
    observe(wf, owner, OPEN)
    examine_all(wf)

    found = reopening(wf, item)

    assert found is not None
    assert [sent.customer for sent in found.sent_to] == ["Acme Corp"]


def test_changes_are_listed_newest_first_with_and_without_answers(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    early = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    # A setting nobody rests on changes: recorded, with no answers.
    observe(wf, owner, {"secret_scanning_enabled": False}, check="github.secret_scanning", at=early)
    observe(
        wf,
        owner,
        {"secret_scanning_enabled": True},
        check="github.secret_scanning",
        at=early + dt.timedelta(hours=1),
    )
    examine_all(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)

    listed = changes(wf, owner)

    assert [entry.change.check_name for entry in listed] == [CHECK, "github.secret_scanning"]
    newest, older = listed
    assert [answer.run_question_id for answer in newest.answers] == [item]
    assert newest.answers[0].waiting is True
    assert (older.answers, [c.fact for c in older.change.changes]) == (
        [],
        ["secret_scanning_enabled"],
    )
    assert older.change.was is not None
    assert len(changes(wf, owner, limit=1)) == 1


def test_a_change_only_an_answer_noticed_is_measured_from_that_answer(wf: Db) -> None:
    # Two readings that agree with each other, and an older answer that rests on neither.
    owner, item, first = approved_answer(wf)
    observe(wf, owner, OPEN, queue=False)
    observe(wf, owner, dict(OPEN))
    examine_all(wf)

    [entry] = changes(wf, owner)

    assert entry.change.was is not None
    assert [c.fact for c in entry.change.changes] == ["protected", "required_approving_reviews"]
    assert item_row(wf, item).reopened_at is not None
    assert first is not None


def test_watched_settings_say_how_the_last_look_went(wf: Db) -> None:
    owner = tenant(wf)
    seen = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)
    first = observe(wf, owner, at=seen, queue=False)
    answers(wf, owner, first, count=2)
    observe(wf, owner, at=seen, scope="acme/unused", queue=False)
    observe(wf, owner, status="unavailable", at=seen + dt.timedelta(days=1))

    [only] = watched(wf, owner)

    assert (only.check_name, only.scope, only.answers) == (CHECK, SCOPE, 2)
    assert (only.last_status, only.last_looked_at) == ("unavailable", seen + dt.timedelta(days=1))
    assert only.last_observed_at == seen
    assert watched(wf, tenant(wf, "another")) == []
