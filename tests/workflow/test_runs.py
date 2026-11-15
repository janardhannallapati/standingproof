"""Runs and leases in a real Postgres: who may work on a question, and how a run ends."""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest

from standingproof.agent.types import AnswerDraft, Usage
from standingproof.models import STATUS_NEEDS_MAPPING, Run
from standingproof.workflow import leases, runs, sweep
from standingproof.workflow.exact import find_exact_answer
from standingproof.workflow.runs import QuestionnaireNotReadyError
from standingproof.workflow.state import Outcome
from tests.conftest import Db
from tests.workflow.seed import (
    approved_answer,
    expire_lease,
    finished_days_ago,
    items,
    questionnaire,
    sql,
    start,
    tenant,
    wf,
)

__all__ = ["wf"]

QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Do you test backups?"]


def outcome(status: str = "ready", cost: float = 0.02, reason: str | None = None) -> Outcome:
    draft = AnswerDraft(status="answered", answer="Yes.", citations=["answer:1"], note="n")
    return Outcome(
        status=status,  # type: ignore[arg-type]
        path="drafted",
        reason=reason,
        draft=draft if status == "ready" else None,
        steps=2,
        cost_usd=cost,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def call(db: Db, function: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        async with db.sessions() as session:
            return await function(session, **arguments)

    return db.run(go())


def take(
    db: Db, item_id: uuid.UUID, owner: str = "w1", seconds: float = 60, reserve: float = 0.0
) -> Any:
    return call(
        db,
        leases.take_lease,
        item_id=item_id,
        owner=owner,
        seconds=seconds,
        max_attempts=3,
        reserve_usd=reserve,
    )


def run_row(db: Db, run_id: uuid.UUID) -> Run:
    async def load() -> Run:
        async with db.sessions() as session:
            return await session.get(Run, run_id)  # type: ignore[return-value]

    return db.run(load())


def test_starting_a_run_queues_one_row_per_question_in_sheet_order(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))

    rows = items(wf, started.run.id)

    assert started.created is True
    assert started.run.status == "running"
    assert [row.status for row in rows] == ["queued"] * 3
    assert [row.position for row in rows] == [1, 2, 3]
    assert [row.id for row in rows] == started.item_ids


def test_starting_again_returns_the_active_run(wf: Db) -> None:
    owner = tenant(wf)
    sheet = questionnaire(wf, owner, QUESTIONS)
    first = start(wf, owner, sheet)

    second = start(wf, owner, sheet)

    assert second.created is False
    assert second.run.id == first.run.id
    assert second.item_ids == []
    assert len(items(wf, first.run.id)) == 3


def test_the_database_refuses_a_second_active_run(wf: Db) -> None:
    owner = tenant(wf)
    sheet = questionnaire(wf, owner, QUESTIONS)
    start(wf, owner, sheet)

    with pytest.raises(Exception, match="uq_runs_one_active"):
        sql(
            wf,
            "INSERT INTO runs (tenant_id, questionnaire_id, status, max_cost_usd) "
            "VALUES (:tenant, :sheet, 'running', 1)",
            tenant=owner,
            sheet=sheet,
        )


def test_a_run_cannot_start_on_another_tenants_questionnaire(wf: Db) -> None:
    owner, other = tenant(wf), tenant(wf, "other")
    sheet = questionnaire(wf, owner, QUESTIONS)

    started = call(
        wf,
        runs.start_run,
        tenant_id=other,
        questionnaire_id=sheet,
        max_cost_usd=5.0,
        enqueue_grace_seconds=60,
    )

    assert started is None


def test_a_run_cannot_start_before_the_columns_are_chosen(wf: Db) -> None:
    owner = tenant(wf)
    sheet = questionnaire(wf, owner, [], status=STATUS_NEEDS_MAPPING)

    with pytest.raises(QuestionnaireNotReadyError):
        start(wf, owner, sheet)


def test_a_run_with_no_questions_is_finished_at_once(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, []))

    assert started.run.status == "finished"
    assert started.run.finished_at is not None


def test_of_two_workers_one_takes_the_question(wf: Db) -> None:
    owner = tenant(wf)
    [item, *_] = start(wf, owner, questionnaire(wf, owner, QUESTIONS)).item_ids

    first = take(wf, item, "w1")
    second = take(wf, item, "w2")

    assert first is not None
    assert first.question == "Is MFA enforced?"
    assert first.tenant_id == owner
    assert first.attempts == 1
    assert second is None


def test_an_expired_lease_can_be_taken_over_and_the_late_result_is_discarded(wf: Db) -> None:
    owner = tenant(wf)
    [item, *_] = start(wf, owner, questionnaire(wf, owner, QUESTIONS)).item_ids
    take(wf, item, "w1")
    expire_lease(wf, item)

    taken = take(wf, item, "w2")
    late = call(wf, leases.save_result, item_id=item, owner="w1", outcome=outcome("needs_review"))
    saved = call(wf, leases.save_result, item_id=item, owner="w2", outcome=outcome("ready"))

    assert taken is not None and taken.attempts == 2
    assert late is False
    assert saved is True
    [row, *_] = items(wf, taken.run_id)
    assert row.status == "ready"
    assert row.answer == "Yes."
    assert row.citations == ["answer:1"]
    assert row.steps == 2
    assert row.cost_usd == Decimal("0.020000")
    assert row.lease_owner is None


def test_a_lease_is_renewed_only_by_its_holder(wf: Db) -> None:
    owner = tenant(wf)
    [item, *_] = start(wf, owner, questionnaire(wf, owner, QUESTIONS)).item_ids
    take(wf, item, "w1")

    assert call(wf, leases.renew_lease, item_id=item, owner="w1", seconds=60) is True
    assert call(wf, leases.renew_lease, item_id=item, owner="w2", seconds=60) is False


def test_the_run_finishes_when_its_last_question_does(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:2]))

    for index, item in enumerate(started.item_ids):
        take(wf, item)
        assert run_row(wf, started.run.id).status == "running"
        call(wf, leases.save_result, item_id=item, owner="w1", outcome=outcome())
        expected = "finished" if index == 1 else "running"
        assert run_row(wf, started.run.id).status == expected

    assert run_row(wf, started.run.id).finished_at is not None


def test_no_question_starts_once_the_run_has_spent_its_ceiling(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS), max_cost_usd=0.05)
    first, second, third = started.item_ids
    take(wf, first)
    call(wf, leases.save_result, item_id=first, owner="w1", outcome=outcome(cost=0.05))

    assert take(wf, second) is None
    assert take(wf, third) is None

    rows = items(wf, started.run.id)
    assert [row.status for row in rows] == ["ready", "not_started", "not_started"]
    assert rows[1].reason == "run_cost_limit"
    assert run_row(wf, started.run.id).status == "finished"


def test_a_question_given_back_is_retried_then_failed_after_the_last_attempt(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:1]))
    [item] = started.item_ids

    for attempt in (1, 2, 3):
        lease = take(wf, item)
        assert lease is not None and lease.attempts == attempt
        call(
            wf,
            leases.give_back,
            item_id=item,
            owner="w1",
            reason="model_unavailable",
            retry_in_seconds=0,
            max_attempts=3,
        )
        [row] = items(wf, started.run.id)
        assert row.status == ("failed" if attempt == 3 else "queued")

    assert row.reason == "model_unavailable"
    assert run_row(wf, started.run.id).status == "finished"


def test_a_question_that_keeps_killing_workers_is_given_up(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:1]))
    [item] = started.item_ids
    for _ in range(3):
        assert take(wf, item) is not None
        expire_lease(wf, item)

    assert take(wf, item) is None

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason) == ("failed", "attempts_exhausted")


def test_the_sweep_finds_open_questions_that_are_due_and_does_not_return_them_twice(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS), enqueue_grace_seconds=0)
    queued, dead, held = started.item_ids
    take(wf, dead)
    expire_lease(wf, dead)
    take(wf, held)

    first = call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60)
    second = call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60)

    assert set(first) == {queued, dead}
    assert second == []


def test_a_question_inside_its_grace_period_is_left_to_the_first_job(wf: Db) -> None:
    owner = tenant(wf)
    start(wf, owner, questionnaire(wf, owner, QUESTIONS), enqueue_grace_seconds=60)

    assert call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60) == []


def test_cancelling_closes_queued_questions_and_lets_running_ones_finish(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))
    running, queued, _ = started.item_ids
    take(wf, running)

    assert call(wf, runs.cancel_run, tenant_id=owner, run_id=started.run.id) is True
    assert take(wf, queued) is None
    assert call(wf, leases.save_result, item_id=running, owner="w1", outcome=outcome()) is True

    rows = items(wf, started.run.id)
    assert [row.status for row in rows] == ["ready", "not_started", "not_started"]
    assert rows[1].reason == "cancelled"
    assert run_row(wf, started.run.id).status == "cancelled"


def test_another_tenant_cannot_read_or_cancel_a_run(wf: Db) -> None:
    owner, other = tenant(wf), tenant(wf, "other")
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))

    assert call(wf, runs.summary, tenant_id=other, run_id=started.run.id) is None
    assert call(wf, runs.cancel_run, tenant_id=other, run_id=started.run.id) is False
    assert (
        call(
            wf,
            runs.page_of_questions,
            tenant_id=other,
            run_id=started.run.id,
            status=None,
            after=0,
            limit=10,
        )
        == []
    )


def test_the_summary_counts_by_status_and_adds_the_cost(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))
    first, second, _ = started.item_ids
    for item, result in ((first, outcome(cost=0.02)), (second, outcome("needs_review", 0.03, "x"))):
        take(wf, item)
        call(wf, leases.save_result, item_id=item, owner="w1", outcome=result)

    found = call(wf, runs.summary, tenant_id=owner, run_id=started.run.id)

    assert found.counts == {"ready": 1, "needs_review": 1, "queued": 1}
    assert found.cost_usd == Decimal("0.05")


def test_old_finished_questions_are_listed_for_pruning_once(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:2]))
    old, recent = started.item_ids
    for item in (old, recent):
        take(wf, item)
        call(wf, leases.save_result, item_id=item, owner="w1", outcome=outcome())
    finished_days_ago(wf, old, 8)

    listed = call(wf, runs.finished_with_state, keep_days=7, limit=10)
    call(wf, runs.mark_state_deleted, item_ids=listed)

    assert listed == [old]
    assert call(wf, runs.finished_with_state, keep_days=7, limit=10) == []


# --- The exact-match shortcut. ---


def exact(db: Db, tenant_id: uuid.UUID, question: str) -> Any:
    return call(db, find_exact_answer, tenant_id=tenant_id, question=question)


def test_the_same_question_in_other_case_and_spacing_matches(wf: Db) -> None:
    owner = tenant(wf)
    answer_id = approved_answer(wf, owner, "Is MFA  enforced?", "Yes, for all staff.")

    match = exact(wf, owner, "  is mfa enforced?\n")

    assert match is not None
    assert match.source_id == f"answer:{answer_id}"
    assert match.answer == "Yes, for all staff."


def test_a_question_worded_differently_does_not_match(wf: Db) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes.")

    assert exact(wf, owner, "Is MFA enforced for contractors?") is None


def test_an_expired_answer_does_not_match(wf: Db) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes.", expires_in_days=-1)

    assert exact(wf, owner, "Is MFA enforced?") is None


def test_another_tenants_answer_does_not_match(wf: Db) -> None:
    owner, other = tenant(wf), tenant(wf, "other")
    approved_answer(wf, other, "Is MFA enforced?", "Yes.")

    assert exact(wf, owner, "Is MFA enforced?") is None


def test_two_approved_answers_that_disagree_are_not_reused(wf: Db) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes.")
    approved_answer(wf, owner, "is mfa enforced?", "Only for administrators.")

    assert exact(wf, owner, "Is MFA enforced?") is None


def test_a_question_full_of_sql_is_only_text(wf: Db) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes.")

    assert exact(wf, owner, "x' OR '1'='1") is None


def test_two_last_questions_finishing_at_the_same_moment_still_finish_the_run(wf: Db) -> None:
    """Each transaction sees the other's question as still open; one must look again."""
    owner = tenant(wf)

    async def save(item: uuid.UUID) -> None:
        async with wf.sessions() as session:
            await leases.save_result(session, item_id=item, owner="w1", outcome=outcome())

    for round_number in range(10):
        sheet = questionnaire(wf, owner, QUESTIONS[:2])
        started = start(wf, owner, sheet)
        for item in started.item_ids:
            take(wf, item)

        async def both(ids: list[uuid.UUID] = started.item_ids) -> None:
            await asyncio.gather(*(save(item) for item in ids))

        wf.run(both())

        assert run_row(wf, started.run.id).status == "finished", f"round {round_number}"


def test_the_sweep_closes_a_run_whose_questions_are_all_final(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:1]))
    sql(wf, "UPDATE run_questions SET status = 'ready', finished_at = now()")

    closed = call(wf, sweep.finish_idle_runs)

    assert closed == 1
    assert run_row(wf, started.run.id).status == "finished"
    assert call(wf, sweep.finish_idle_runs) == 0


# --- The run's cost ceiling counts work in progress and work that did not finish. ---


def test_a_question_waits_while_those_in_progress_could_use_up_the_budget(wf: Db) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS), max_cost_usd=0.50)
    first, second, _ = started.item_ids
    assert take(wf, first, reserve=0.50) is not None

    # Nothing is spent yet, but the first question may still use the whole 0.50.
    held_back = take(wf, second, "w2", reserve=0.50)

    rows = items(wf, started.run.id)
    assert held_back is None
    assert rows[1].status == "queued"  # waiting, not closed
    assert call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60) == []

    call(wf, leases.save_result, item_id=first, owner="w1", outcome=outcome(cost=0.02))
    assert take(wf, second, "w2", reserve=0.50) is not None


def test_two_workers_cannot_both_take_the_last_room_in_the_budget(wf: Db) -> None:
    owner = tenant(wf)

    async def try_take(item: uuid.UUID, name: str) -> Any:
        async with wf.sessions() as session:
            return await leases.take_lease(
                session, item_id=item, owner=name, seconds=60, max_attempts=3, reserve_usd=0.50
            )

    for round_number in range(8):
        started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:2]), max_cost_usd=0.50)
        first, second = started.item_ids

        async def both(a: uuid.UUID = first, b: uuid.UUID = second) -> list[Any]:
            return list(await asyncio.gather(try_take(a, "w1"), try_take(b, "w2")))

        taken = [lease for lease in wf.run(both()) if lease is not None]

        assert len(taken) == 1, f"round {round_number}"


def test_money_spent_on_a_question_that_was_given_back_counts_against_the_run(wf: Db) -> None:
    from standingproof.workflow.state import Progress

    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:2]), max_cost_usd=0.05)
    first, second = started.item_ids
    take(wf, first)

    call(
        wf,
        leases.give_back,
        item_id=first,
        owner="w1",
        reason="model_unavailable",
        retry_in_seconds=0,
        max_attempts=3,
        progress=Progress(steps=2, cost_usd=0.06, usage=Usage(input_tokens=9)),
    )

    rows = items(wf, started.run.id)
    assert (rows[0].status, rows[0].steps, rows[0].cost_usd) == ("queued", 2, Decimal("0.060000"))
    assert take(wf, second) is None
    assert items(wf, started.run.id)[1].reason == "run_cost_limit"


def test_a_dead_workers_question_is_due_as_soon_as_its_lease_runs_out(wf: Db) -> None:
    owner = tenant(wf)
    # Still inside the grace period given to the first job when the worker dies.
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS[:1]), enqueue_grace_seconds=60)
    [item] = started.item_ids
    take(wf, item)
    assert call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60) == []

    expire_lease(wf, item)

    assert call(wf, sweep.due_for_queue, limit=10, requeue_seconds=60) == [item]
