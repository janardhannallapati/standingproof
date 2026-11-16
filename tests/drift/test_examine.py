"""A new observation of a setting, compared with the answers that rest on it."""

import asyncio
import uuid

import pytest

from standingproof.drift import examine
from tests.conftest import Db
from tests.drift.rig import (
    CHECK,
    OPEN,
    PROTECTED,
    SCOPE,
    answers,
    approved_answer,
    events,
    examine_all,
    item_row,
    observe,
    queued,
    reopenings,
    rests_on,
)
from tests.workflow.seed import items, tenant, wf
from tests.workflow.test_review import decide

__all__ = ["wf"]


def test_a_saved_draft_records_the_observation_it_cites(wf: Db) -> None:
    owner = tenant(wf)
    seen = observe(wf, owner, queue=False)
    failed = observe(wf, owner, status="unavailable", scope="acme/other")

    _, [item] = answers(wf, owner, seen)
    assert rests_on(wf, item) == [(CHECK, SCOPE, seen)]

    # A redraft that cites nothing live, or a look that saw nothing, rests on no setting.
    wf.run(_redraft(wf, owner, item, [f"evidence:{failed}", "answer:x", "evidence:not-a-uuid"]))
    assert rests_on(wf, item) == []


async def _redraft(db: Db, owner: uuid.UUID, item: uuid.UUID, citations: list[str]) -> None:
    from standingproof.drift.watch import rest_on

    async with db.sessions() as session:
        await rest_on(session, owner, item, citations)
        await session.commit()


def test_an_approved_answer_is_reopened_when_its_setting_reads_differently(wf: Db) -> None:
    owner, item, first = approved_answer(wf)
    before = item_row(wf, item)
    now = observe(wf, owner, OPEN, trigger="webhook")

    reopened_runs = examine_all(wf)

    after = item_row(wf, item)
    assert reopened_runs == [after.run_id]
    assert (after.review, after.version) == ("pending", before.version + 1)
    assert after.reopened_at is not None
    # What the agent concluded and what was approved are untouched.
    assert (after.status, after.reason, after.final_answer) == ("ready", None, "Yes, two reviews.")
    [event] = events(wf)
    assert (event.check_name, event.scope, event.evidence_id) == (CHECK, SCOPE, now)
    assert (event.previous_evidence_id, event.trigger) == (first, "webhook")
    [reopening] = reopenings(wf)
    assert (reopening.run_question_id, reopening.was_evidence_id) == (item, first)
    assert reopening.was_review == "approved"
    assert reopening.changes == [
        {"fact": "protected", "was": True, "now": False},
        {"fact": "required_approving_reviews", "was": 2, "now": 0},
    ]
    assert rests_on(wf, item) == [(CHECK, SCOPE, now)]
    assert queued(wf) == 0


def test_the_same_reading_reopens_nothing_and_records_no_event(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    observe(wf, owner, dict(PROTECTED))

    assert examine_all(wf) == []
    assert (item_row(wf, item).review, events(wf), queued(wf)) == ("approved", [], 0)


def test_a_look_that_saw_nothing_changes_nothing(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    observe(wf, owner, status="unavailable")

    assert examine_all(wf) == []
    assert (item_row(wf, item).review, events(wf)) == ("approved", [])


def test_examining_again_adds_nothing(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    changed = observe(wf, owner, OPEN)
    examine_all(wf)
    version = item_row(wf, item).version

    # The same observation is queued a second time (a retry after a lost reply).
    wf.run(_requeue(wf, owner, changed))
    assert examine_all(wf) == []

    assert (len(events(wf)), len(reopenings(wf))) == (1, 1)
    assert item_row(wf, item).version == version


async def _requeue(db: Db, owner: uuid.UUID, evidence_id: uuid.UUID) -> None:
    from standingproof.models import DriftQueue

    async with db.sessions() as session:
        session.add(DriftQueue(evidence_id=evidence_id, tenant_id=owner, trigger="schedule"))
        await session.commit()


def test_two_examiners_at_once_reopen_an_answer_once(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    for _ in range(3):
        observe(wf, owner, OPEN)

    async def both() -> None:
        await asyncio.gather(*(examine.examine_pending(wf.sessions, limit=10) for _ in range(4)))

    wf.run(both())

    assert (len(events(wf)), len(reopenings(wf)), queued(wf)) == (1, 1, 0)
    assert item_row(wf, item).review == "pending"


def test_dying_before_the_commit_leaves_nothing_and_the_next_tick_does_the_work(
    wf: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, item, _ = approved_answer(wf)
    observe(wf, owner, OPEN)
    reopen = examine._reopen

    async def reopens_then_dies(*args: object, **kwargs: object) -> None:
        await reopen(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("the process died here")

    monkeypatch.setattr(examine, "_reopen", reopens_then_dies)
    with pytest.raises(RuntimeError):
        examine_all(wf)
    assert (item_row(wf, item).review, events(wf), reopenings(wf), queued(wf)) == (
        "approved",
        [],
        [],
        1,
    )

    monkeypatch.setattr(examine, "_reopen", reopen)
    examine_all(wf)
    assert (item_row(wf, item).review, len(reopenings(wf)), queued(wf)) == ("pending", 1, 0)


def test_a_draft_still_waiting_is_marked_and_cannot_be_approved_unread(wf: Db) -> None:
    from standingproof.workflow import review
    from tests.workflow.test_runs import call

    owner = tenant(wf)
    first = observe(wf, owner, queue=False)
    run_id, [cites, other] = answers(wf, owner, first, count=2)
    drafted_other = observe(wf, owner, queue=False, scope="acme/other")
    wf.run(_redraft(wf, owner, other, [f"evidence:{drafted_other}"]))
    observe(wf, owner, OPEN)

    examine_all(wf)

    row = item_row(wf, cites)
    assert (row.review, row.status) == ("pending", "ready")
    assert row.reopened_at is not None
    assert reopenings(wf)[0].was_review is None
    # One question is ready to approve unread, not two.
    with pytest.raises(review.ReviewConflictError):
        call(wf, review.approve_ready, tenant_id=owner, run_id=run_id, count=2, reviewer="asha")
    assert (
        call(wf, review.approve_ready, tenant_id=owner, run_id=run_id, count=1, reviewer="a") == 1
    )
    assert [row.review for row in items(wf, run_id)] == ["pending", "approved"]


def test_rejected_and_unanswered_questions_and_other_tenants_are_untouched(wf: Db) -> None:
    owner = tenant(wf)
    first = observe(wf, owner, queue=False)
    run_id, [rejected, unanswered, kept] = answers(wf, owner, first, count=3)
    decide(wf, owner, rejected, "reject")
    wf.run(_fail(wf, unanswered))
    decide(wf, owner, kept, "edit", answer="Yes. Two reviews are required.")
    other, theirs, _ = approved_answer(wf, "another")

    observe(wf, owner, OPEN)
    examine_all(wf)

    assert [(row.review, row.reopened_at is None) for row in items(wf, run_id)] == [
        ("rejected", True),
        (None, True),
        ("pending", False),
    ]
    assert [r.was_review for r in reopenings(wf)] == ["edited"]
    assert item_row(wf, theirs).review == "approved"
    assert other != owner


async def _fail(db: Db, item: uuid.UUID) -> None:
    from sqlalchemy import update

    from standingproof.models import RunQuestion

    async with db.sessions() as session:
        await session.execute(
            update(RunQuestion).where(RunQuestion.id == item).values(status="failed", review=None)
        )
        await session.commit()


def test_an_answer_whose_facts_already_differ_is_caught_at_the_first_look(wf: Db) -> None:
    # The setting changed before this feature existed: two observations, no event.
    owner, item, first = approved_answer(wf)
    observe(wf, owner, OPEN, queue=False)
    assert examine_all(wf) == []

    latest = observe(wf, owner, dict(OPEN))
    examine_all(wf)

    assert item_row(wf, item).review == "pending"
    [event] = events(wf)
    # The observation before it reads the same, so the event names no previous one.
    assert (event.evidence_id, event.previous_evidence_id) == (latest, None)
    assert reopenings(wf)[0].was_evidence_id == first


def test_a_change_and_a_change_back_while_waiting_are_two_rows_and_still_waiting(wf: Db) -> None:
    owner, item, first = approved_answer(wf)
    changed = observe(wf, owner, OPEN)
    examine_all(wf)
    since = item_row(wf, item).reopened_at
    observe(wf, owner, dict(PROTECTED))
    examine_all(wf)

    row = item_row(wf, item)
    assert (row.review, row.reopened_at) == ("pending", since)
    first_row, second_row = reopenings(wf)
    assert (first_row.was_evidence_id, first_row.was_review) == (first, "approved")
    # The second is measured from what the reviewer was last shown, and restores nothing.
    assert (second_row.was_evidence_id, second_row.was_review) == (changed, None)


def test_the_event_says_why_the_newest_look_was_made_whichever_row_is_examined_first(
    wf: Db,
) -> None:
    owner, _, _ = approved_answer(wf)
    # An agent's look is still queued when a webhook's look sees the change.
    observe(wf, owner, dict(PROTECTED), trigger="run")
    observe(wf, owner, OPEN, trigger="webhook")

    examine_all(wf)

    assert [event.trigger for event in events(wf)] == ["webhook"]
