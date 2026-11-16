"""The worker against real Postgres and Redis, with a scripted model."""

import uuid

from tests.conftest import Db
from tests.workflow.rig import CountingModel, Rig, is_rate_limited, says_no_source, take
from tests.workflow.seed import (
    approved_answer,
    expire_lease,
    items,
    questionnaire,
    start,
    tenant,
    wf,
)

__all__ = ["wf"]


def test_a_question_with_an_approved_answer_ends_ready_with_no_model_call(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes, for all staff.")
    started = start(wf, owner, questionnaire(wf, owner, ["is mfa enforced?"]))
    rig = Rig(wf, redis_url, CountingModel())
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.path, row.answer) == ("ready", "reused", "Yes, for all staff.")
    assert row.finished_at is not None
    assert rig.model.calls == 0
    assert wf.run(rig.queue.pending()) == 0
    assert wf.run(rig.redis.xlen(rig.stream)) == 0


def test_a_question_with_no_source_goes_to_a_person_with_the_cost_recorded(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason, row.note) == ("needs_review", "no_approved_source", "Nothing.")
    assert row.steps == 1
    assert row.cost_usd > 0


def test_the_same_job_twice_runs_the_question_once(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())
    rig.add(started.item_ids * 2)

    handled = rig.work_through_the_queue()

    assert handled == 2
    assert rig.model.calls == 1
    assert wf.run(rig.queue.pending()) == 0


def test_a_job_that_names_nothing_is_dropped(wf: Db, redis_url: str) -> None:
    rig = Rig(wf, redis_url, CountingModel())
    wf.run(rig.queue.add(["not-a-uuid", str(uuid.uuid4())]))

    assert rig.work_through_the_queue() == 2
    assert wf.run(rig.queue.pending()) == 0


def test_a_rate_limited_question_is_retried_later_and_fails_after_three_attempts(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel(is_rate_limited))
    rig.add(started.item_ids)

    for attempt in (1, 2, 3):
        rig.work_through_the_queue()
        [row] = items(wf, started.run.id)
        assert row.attempts == attempt
        assert row.status == ("failed" if attempt == 3 else "queued")
        wf.run(rig.worker.sweep())

    assert row.reason == "model_unavailable"
    assert rig.model.calls == 3


def test_a_question_that_recovers_on_the_second_attempt_is_answered(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    model = CountingModel(is_rate_limited)
    rig = Rig(wf, redis_url, model)
    rig.add(started.item_ids)
    rig.work_through_the_queue()

    model.behaviour = says_no_source
    wf.run(rig.worker.sweep())
    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.attempts) == ("needs_review", 2)


def test_the_sweep_queues_a_question_whose_job_was_never_written(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(
        wf, owner, questionnaire(wf, owner, ["Do you test backups?"]), enqueue_grace_seconds=0
    )
    rig = Rig(wf, redis_url, CountingModel())

    assert rig.work_through_the_queue() == 0
    assert wf.run(rig.worker.sweep()) == 1
    assert rig.work_through_the_queue() == 1

    [row] = items(wf, started.run.id)
    assert row.status == "needs_review"
    assert rig.model.calls == 1


def test_a_dead_workers_question_is_finished_by_another_and_its_job_is_cleared(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(
        wf, owner, questionnaire(wf, owner, ["Do you test backups?"]), enqueue_grace_seconds=0
    )
    [item] = started.item_ids
    dead = Rig(wf, redis_url, CountingModel(), name="dead")
    dead.add([item])
    # The dead worker read the job and took the lease, then stopped existing.
    wf.run(dead.queue.read(count=1, block_ms=50))
    wf.run(take(wf, item, "dead"))
    expire_lease(wf, item)

    alive = Rig(wf, redis_url, CountingModel(), name="alive", stream=dead.stream, lease_seconds=0)
    wf.run(alive.worker.sweep())
    alive.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.attempts) == ("needs_review", 2)
    assert wf.run(alive.queue.pending()) == 0
