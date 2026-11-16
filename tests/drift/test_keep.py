"""What a reviewer can do with a reopened answer."""

import uuid
from typing import Any

import pytest

from standingproof.workflow import runs
from standingproof.workflow.review import ReviewConflictError
from tests.conftest import Db
from tests.drift.rig import (
    OPEN,
    PROTECTED,
    answers,
    approved_answer,
    examine_all,
    item_row,
    observe,
)
from tests.workflow.seed import sql, tenant, wf
from tests.workflow.test_review import decide, decisions
from tests.workflow.test_runs import call

__all__ = ["wf"]


def reopened(db: Db, first_decision: str = "approve", **extra: Any) -> tuple[uuid.UUID, uuid.UUID]:
    owner = tenant(db)
    _, [item] = answers(db, owner, observe(db, owner, queue=False))
    decide(db, owner, item, first_decision, **extra)
    observe(db, owner, OPEN)
    examine_all(db)
    return owner, item


def now_decide(db: Db, owner: uuid.UUID, item: uuid.UUID, decision: str, **extra: Any) -> Any:
    return decide(db, owner, item, decision, version=item_row(db, item).version, **extra)


def test_keeping_an_approved_answer_puts_it_back_as_it_was(wf: Db) -> None:
    owner, item = reopened(wf)

    now_decide(wf, owner, item, "keep")

    row = item_row(wf, item)
    assert (row.review, row.final_answer, row.reopened_at) == (
        "approved",
        "Yes, two reviews.",
        None,
    )
    kept = decisions(wf, item)[-1]
    assert (kept.decision, kept.decided_by) == ("keep", "asha")
    # The record says which wording the reviewer was shown and kept.
    assert (kept.shown_answer, kept.final_answer) == ("Yes, two reviews.", "Yes, two reviews.")


def test_keeping_an_edited_answer_keeps_the_edit_not_the_agents_draft(wf: Db) -> None:
    owner, item = reopened(wf, "edit", answer="Yes. Two reviews are required.")

    now_decide(wf, owner, item, "keep")

    row = item_row(wf, item)
    assert (row.review, row.final_answer) == ("edited", "Yes. Two reviews are required.")
    assert row.answer == "Yes, two reviews."
    assert decisions(wf, item)[-1].shown_answer == "Yes. Two reviews are required."


def test_keeping_a_draft_nobody_had_decided_approves_that_draft(wf: Db) -> None:
    owner = tenant(wf)
    _, [item] = answers(wf, owner, observe(wf, owner, queue=False))
    observe(wf, owner, OPEN)
    examine_all(wf)

    now_decide(wf, owner, item, "keep")

    row = item_row(wf, item)
    assert (row.review, row.final_answer, row.reopened_at) == (
        "approved",
        "Yes, two reviews.",
        None,
    )


def test_keeping_after_a_change_and_a_change_back_restores_the_first_state(wf: Db) -> None:
    owner, item = reopened(wf, "edit", answer="Yes. Two reviews are required.")
    observe(wf, owner, dict(PROTECTED))
    examine_all(wf)

    now_decide(wf, owner, item, "keep")

    assert item_row(wf, item).review == "edited"


def test_approve_is_refused_on_a_reopened_answer_and_keep_on_any_other(wf: Db) -> None:
    owner, item = reopened(wf)
    with pytest.raises(ReviewConflictError, match="has changed"):
        now_decide(wf, owner, item, "approve")
    assert item_row(wf, item).review == "pending"

    other, plain, _ = approved_answer(wf, "another")
    sql(wf, "UPDATE run_questions SET review = 'pending' WHERE id = :id", id=plain)
    with pytest.raises(ReviewConflictError, match="not reopened"):
        now_decide(wf, other, plain, "keep")


@pytest.mark.parametrize(
    ("decision", "extra", "review"),
    [
        ("edit", {"answer": "No. Reviews are not enforced today."}, "edited"),
        ("reject", {"comment": "Withdrawn until protection is back."}, "rejected"),
        ("redraft", {"comment": "Look at the setting again."}, "redrafting"),
    ],
)
def test_every_other_decision_also_closes_the_reopening(
    wf: Db, decision: str, extra: dict[str, str], review: str
) -> None:
    owner, item = reopened(wf)

    now_decide(wf, owner, item, decision, **extra)

    row = item_row(wf, item)
    assert (row.review, row.reopened_at) == (review, None)


def test_a_reopened_answer_is_given_its_time_before_it_expires(wf: Db) -> None:
    owner, item = reopened(wf)
    # Approved long ago; reopened today.
    sql(wf, "UPDATE run_questions SET finished_at = now() - interval '40 days'")

    assert call(wf, runs.expire_unreviewed, keep_days=30) == 0
    assert item_row(wf, item).review == "pending"

    sql(wf, "UPDATE run_questions SET reopened_at = now() - interval '31 days'")
    assert call(wf, runs.expire_unreviewed, keep_days=30) == 1
    # Closed for good: it is no longer waiting for anyone, so it is no longer "reopened".
    row = item_row(wf, item)
    assert (row.review, row.reopened_at) == ("expired", None)
