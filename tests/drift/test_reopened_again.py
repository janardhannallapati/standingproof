"""The same change reopens an answer a second time, after a decision closed the first wait.

It happens when a redraft comes to rest on an older observation again: the newest reading
is compared with it and is the change that was already recorded. Each wait keeps a record
of its own, so the reviewer is shown what this draft cites and what the setting reads now.
"""

import uuid

from fastapi.testclient import TestClient

from standingproof.models import AnswerReopening
from tests.conftest import ClientFactory, Db
from tests.drift.rig import OPEN, all_rows, approved_answer, examine_all, item_row, observe
from tests.drift.test_keep import now_decide
from tests.drift.test_view import changes, reopening
from tests.test_stored_order import redrafted_by_the_worker
from tests.workflow.seed import wf

__all__ = ["wf"]


def reopened_again(db: Db) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """An approved answer, reopened, sent back, and redrafted from the first look again."""
    owner, item, first = approved_answer(db)
    newest = observe(db, owner, OPEN)
    examine_all(db)
    now_decide(db, owner, item, "redraft", comment="Shorter, please.")
    redrafted_by_the_worker(db, owner, item, "Yes, 2 reviews.", [f"evidence:{first}"])
    examine_all(db)
    return owner, item, first, newest


def test_the_second_wait_has_a_record_of_its_own(wf: Db) -> None:
    _, item, first, _ = reopened_again(wf)

    rows = sorted(
        (row for row in all_rows(wf, AnswerReopening) if row.run_question_id == item),
        key=lambda row: row.seq,
    )

    # The first says what was approved; it is not touched. The second says what this draft
    # cites, and that nobody had decided it.
    assert [(row.was_review, row.was_evidence_id) for row in rows] == [
        ("approved", first),
        (None, first),
    ]
    assert rows[0].drift_event_id == rows[1].drift_event_id
    assert item_row(wf, item).reopened_seq == rows[1].seq


def test_the_reviewer_is_shown_what_the_draft_cites_and_what_the_setting_reads_now(
    wf: Db,
) -> None:
    _, item, first, newest = reopened_again(wf)

    found = reopening(wf, item)

    assert found is not None
    [setting] = found.settings
    assert found.was_review is None
    # From the look the draft cites (protected) to the newest (not protected).
    assert setting.was is not None and "'protected': True" in setting.was.statement
    assert "'protected': False" in setting.now.statement
    assert {change.fact: (change.was, change.now) for change in setting.changes}["protected"] == (
        True,
        False,
    )
    assert first != newest


def test_a_change_lists_an_answer_once_however_often_it_reopened_it(wf: Db) -> None:
    owner, item, _, _ = reopened_again(wf)

    [entry] = [each for each in changes(wf, owner) if each.answers]

    assert [answer.run_question_id for answer in entry.answers] == [item]
    assert entry.answers[0].waiting is True


def test_keeping_in_the_second_wait_approves_the_draft_on_the_screen(wf: Db) -> None:
    owner, item, _, _ = reopened_again(wf)

    now_decide(wf, owner, item, "keep")

    kept = item_row(wf, item)
    assert (kept.review, kept.final_answer, kept.reopened_at) == (
        "approved",
        "Yes, 2 reviews.",
        None,
    )


def test_the_api_gives_the_question_its_reopening(make_api: ClientFactory, wf: Db) -> None:
    _, item, _, _ = reopened_again(wf)
    api: TestClient = make_api()

    question = api.get(f"/run-questions/{item}").json()

    # What the screen draws its "What changed" block and its "Keep" button from.
    assert question["reopening"] is not None
    assert question["reopening"]["was_review"] is None
    assert len(question["reopening"]["settings"]) == 1


def test_a_redraft_that_fails_after_a_reopened_wait_comes_back_reopened(wf: Db) -> None:
    from standingproof.workflow import results
    from tests.workflow.test_runs import take

    owner, item, _ = approved_answer(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)
    now_decide(wf, owner, item, "redraft", comment="Shorter, please.")
    assert take(wf, item) is not None

    async def the_model_is_away() -> None:
        async with wf.database.tenant(owner)() as session:
            await results.give_back(
                session,
                item_id=item,
                owner="w1",
                reason="model_unavailable",
                retry_in_seconds=0,
                max_attempts=1,
            )

    wf.run(the_model_is_away())
    examine_all(wf)

    # The earlier draft stands again, and it was written from the look before the change.
    # It must not come back as an ordinary draft that one "approve" accepts.
    row = item_row(wf, item)
    assert (row.review, row.reason, row.reopened_at is not None) == (
        "pending",
        "redraft_failed",
        True,
    )
    found = reopening(wf, item)
    assert found is not None and found.was_review is None
    [setting] = found.settings
    assert {change.fact: (change.was, change.now) for change in setting.changes}["protected"] == (
        True,
        False,
    )
