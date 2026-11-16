"""Recording a person's decision, in a real Postgres."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from standingproof.agent.types import AnswerDraft, Usage
from standingproof.models import ReviewDecision
from standingproof.workflow import results, review, runs
from standingproof.workflow.review import InvalidDecisionError, ReviewConflictError
from standingproof.workflow.state import Outcome
from tests.conftest import Db
from tests.workflow.seed import approved_answer, items, questionnaire, start, tenant, wf
from tests.workflow.test_runs import call, run_row, take

__all__ = ["wf"]

QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Do you test backups?"]


def drafted(
    db: Db,
    item: uuid.UUID,
    *,
    answer: str = "Yes.",
    citations: list[str] | None = None,
    status: str = "ready",
) -> None:
    """Put a question where a worker leaves it: drafted, waiting for a person."""
    has_draft = status == "ready"
    outcome = Outcome(
        status=status,  # type: ignore[arg-type]
        path="drafted" if has_draft else None,
        reason=None if has_draft else "no_approved_source",
        draft=AnswerDraft(status="answered", answer=answer, citations=citations or [], note="")
        if has_draft
        else None,
        steps=2,
        cost_usd=0.02,
        usage=Usage(),
    )
    assert take(db, item) is not None
    assert call(db, results.save_result, item_id=item, owner="w1", outcome=outcome)


def decide(
    db: Db, owner: uuid.UUID, item: uuid.UUID, decision: str, version: int = 1, **extra: Any
) -> Any:
    return call(
        db,
        review.decide,
        tenant_id=owner,
        item_id=item,
        decision=decision,
        version=version,
        reviewer="asha",
        max_redrafts=2,
        max_chars=4000,
        **extra,
    )


def decisions(db: Db, item: uuid.UUID) -> list[ReviewDecision]:
    async def load() -> list[ReviewDecision]:
        async with db.sessions() as session:
            rows = await session.execute(
                select(ReviewDecision)
                .where(ReviewDecision.run_question_id == item)
                .order_by(ReviewDecision.decided_at)
            )
            return list(rows.scalars())

    return db.run(load())


def one_drafted(db: Db, **kwargs: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner = tenant(db)
    started = start(db, owner, questionnaire(db, owner, QUESTIONS[:1]))
    [item] = started.item_ids
    drafted(db, item, **kwargs)
    return owner, started.run.id, item


def test_a_finished_question_waits_for_a_person_at_version_one(wf: Db) -> None:
    _, run_id, _ = one_drafted(wf)

    [row] = items(wf, run_id)

    assert (row.status, row.review, row.version) == ("ready", "pending", 1)


def test_approving_records_who_when_and_the_exact_wording(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf, answer="Yes, for all staff.")

    decided = decide(wf, owner, item, "approve")

    assert decided.warnings == []
    [row] = items(wf, run_id)
    assert (row.review, row.final_answer, row.version) == ("approved", "Yes, for all staff.", 2)
    assert row.reviewed_at is not None
    [record] = decisions(wf, item)
    assert (record.decision, record.decided_by, record.batch) == ("approve", "asha", False)
    assert record.shown_answer == record.final_answer == "Yes, for all staff."
    assert record.decided_at is not None


def test_editing_keeps_the_draft_and_approves_the_reviewers_wording(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf, answer="Yes.")

    decide(wf, owner, item, "edit", answer="  Yes, including contractors.  ")

    [row] = items(wf, run_id)
    assert row.review == "edited"
    assert row.answer == "Yes."
    assert row.final_answer == "Yes, including contractors."
    [record] = decisions(wf, item)
    assert (record.shown_answer, record.final_answer) == ("Yes.", "Yes, including contractors.")


def test_an_edit_that_claims_more_than_its_sources_say_is_warned_about_not_blocked(wf: Db) -> None:
    owner = tenant(wf)
    source = approved_answer(wf, owner, "Is MFA enforced?", "Yes, for all staff.")
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:1]))
    [item] = started.item_ids
    drafted(wf, item, citations=[f"answer:{source}"])

    decided = decide(wf, owner, item, "edit", answer="Yes. We are ISO 27001 certified.")

    assert decided.warnings == ["certified"]
    assert items(wf, started.run.id)[0].review == "edited"


def test_rejecting_leaves_no_final_answer_and_keeps_the_reason(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf)

    decide(wf, owner, item, "reject", comment="Legal must answer this one.")

    [row] = items(wf, run_id)
    assert (row.review, row.final_answer) == ("rejected", None)
    assert decisions(wf, item)[0].comment == "Legal must answer this one."


def test_a_question_with_no_draft_cannot_be_approved_only_written(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf, status="needs_review")

    with pytest.raises(InvalidDecisionError, match="no draft"):
        decide(wf, owner, item, "approve")
    decide(wf, owner, item, "edit", answer="We carry cover of 2 million.")

    assert items(wf, run_id)[0].final_answer == "We carry cover of 2 million."


@pytest.mark.parametrize(
    ("decision", "extra"),
    [
        ("edit", {"answer": "   "}),
        ("edit", {"answer": "x" * 4001}),
        ("redraft", {}),
        ("redraft", {"comment": "  "}),
    ],
)
def test_a_decision_missing_what_it_needs_is_refused_and_nothing_changes(
    wf: Db, decision: str, extra: dict[str, str]
) -> None:
    owner, run_id, item = one_drafted(wf)

    with pytest.raises(InvalidDecisionError):
        decide(wf, owner, item, decision, **extra)

    assert items(wf, run_id)[0].review == "pending"
    assert decisions(wf, item) == []


def test_a_decision_on_an_older_version_is_refused(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf)

    with pytest.raises(ReviewConflictError, match="changed"):
        decide(wf, owner, item, "approve", version=0)

    assert items(wf, run_id)[0].review == "pending"


def test_a_question_already_decided_cannot_be_decided_again(wf: Db) -> None:
    owner, _, item = one_drafted(wf)
    decide(wf, owner, item, "approve")

    with pytest.raises(ReviewConflictError, match="not waiting"):
        decide(wf, owner, item, "reject", version=2)

    assert len(decisions(wf, item)) == 1


def test_of_two_reviewers_deciding_at_once_one_is_refused(wf: Db) -> None:
    owner, _, item = one_drafted(wf)

    async def one(decision: str) -> Any:
        async with wf.sessions() as session:
            try:
                return await review.decide(
                    session,
                    tenant_id=owner,
                    item_id=item,
                    decision=decision,  # type: ignore[arg-type]
                    version=1,
                    reviewer=decision,
                    max_redrafts=2,
                    max_chars=4000,
                )
            except ReviewConflictError as error:
                return error

    async def both() -> list[Any]:
        return list(await asyncio.gather(one("approve"), one("reject")))

    outcomes = wf.run(both())

    assert sum(isinstance(o, ReviewConflictError) for o in outcomes) == 1
    assert len(decisions(wf, item)) == 1


def test_another_tenant_cannot_decide(wf: Db) -> None:
    _, run_id, item = one_drafted(wf)
    other = tenant(wf, "other")

    assert decide(wf, other, item, "approve") is None
    assert items(wf, run_id)[0].review == "pending"


def test_a_redraft_queues_the_question_and_reopens_its_run(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf)
    assert run_row(wf, run_id).status == "finished"

    decide(wf, owner, item, "redraft", comment="Mention contractors.")

    [row] = items(wf, run_id)
    assert (row.status, row.review, row.redrafts, row.attempts) == ("queued", "redrafting", 1, 0)
    assert row.answer == "Yes."  # the earlier draft stays until a new one replaces it
    assert run_row(wf, run_id).status == "running"
    lease = take(wf, item)
    assert lease.resume == {"decision": "redraft", "instruction": "Mention contractors."}


def test_a_question_can_be_sent_back_twice_and_no_more(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf)
    for round_number in (1, 2):
        version = items(wf, run_id)[0].version
        decide(wf, owner, item, "redraft", version=version, comment=f"Round {round_number}.")
        drafted(wf, item)

    version = items(wf, run_id)[0].version
    with pytest.raises(ReviewConflictError, match="already been redrafted 2 times"):
        decide(wf, owner, item, "redraft", version=version, comment="Again.")

    assert items(wf, run_id)[0].review == "pending"


def test_a_redraft_is_refused_when_a_newer_run_is_active(wf: Db) -> None:
    owner = tenant(wf)
    sheet = questionnaire(wf, owner, QUESTIONS[:1])
    first = start(wf, owner, sheet)
    [item] = first.item_ids
    drafted(wf, item)
    start(wf, owner, sheet)  # the first run had finished, so this is a second, active one

    with pytest.raises(ReviewConflictError, match="newer run"):
        decide(wf, owner, item, "redraft", comment="Mention contractors.")

    [row] = items(wf, first.run.id)
    assert (row.status, row.review) == ("ready", "pending")
    assert decisions(wf, item) == []


def test_a_redraft_that_cannot_be_made_puts_the_earlier_draft_back_for_review(wf: Db) -> None:
    owner, run_id, item = one_drafted(wf, answer="Yes.")
    decide(wf, owner, item, "redraft", comment="Mention contractors.")

    for _ in range(3):
        assert take(wf, item) is not None
        call(
            wf,
            results.give_back,
            item_id=item,
            owner="w1",
            reason="model_unavailable",
            retry_in_seconds=0,
            max_attempts=3,
        )

    [row] = items(wf, run_id)
    assert (row.status, row.reason, row.review) == ("needs_review", "redraft_failed", "pending")
    assert row.answer == "Yes."
    assert run_row(wf, run_id).status == "finished"


def test_approve_ready_approves_only_ready_questions_still_waiting(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))
    ready_one, ready_two, needs = started.item_ids
    drafted(wf, ready_one, answer="One.")
    drafted(wf, ready_two, answer="Two.")
    drafted(wf, needs, status="needs_review")
    decide(wf, owner, ready_one, "reject")

    approved = call(
        wf, review.approve_ready, tenant_id=owner, run_id=started.run.id, count=1, reviewer="asha"
    )

    rows = items(wf, started.run.id)
    assert approved == 1
    assert [row.review for row in rows] == ["rejected", "approved", "pending"]
    assert rows[1].final_answer == "Two."
    [record] = decisions(wf, ready_two)
    assert (record.decision, record.batch, record.decided_by) == ("approve", True, "asha")


def test_approve_ready_does_nothing_when_the_count_is_not_what_was_shown(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:2]))
    for item in started.item_ids:
        drafted(wf, item)

    with pytest.raises(ReviewConflictError, match="2 questions are ready now, not 1"):
        call(
            wf,
            review.approve_ready,
            tenant_id=owner,
            run_id=started.run.id,
            count=1,
            reviewer="asha",
        )

    assert [row.review for row in items(wf, started.run.id)] == ["pending", "pending"]


def test_approve_ready_on_another_tenants_run_is_not_found(wf: Db) -> None:
    _, run_id, _ = one_drafted(wf)
    other = tenant(wf, "other")

    assert (
        call(wf, review.approve_ready, tenant_id=other, run_id=run_id, count=1, reviewer="x")
        is None
    )
    assert call(wf, runs.summary, tenant_id=other, run_id=run_id) is None
