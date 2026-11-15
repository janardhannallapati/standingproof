"""The worker against real Postgres and Redis, with a scripted model."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from redis.asyncio import Redis

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import AnswerDraft, ModelTurn
from standingproof.config import Settings
from standingproof.workflow.graph import build_graph
from standingproof.workflow.queue import Job, JobQueue
from standingproof.workflow.worker import Worker, WorkerDeps
from tests.agent.fakes import finishes
from tests.conftest import Db, no_backends
from tests.fakes import FakeEmbedder
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

NO_SOURCE = AnswerDraft(status="no_approved_source", answer="", citations=[], note="Nothing.")
Behaviour = Callable[[], Awaitable[ModelTurn]]


async def says_no_source() -> ModelTurn:
    return finishes(NO_SOURCE)


async def is_rate_limited() -> ModelTurn:
    raise ModelCallError("rate limited", retryable=True)


class CountingModel:
    """Applies one behaviour to every call and records how many ran at once."""

    def __init__(self, behaviour: Behaviour = says_no_source) -> None:
        self.behaviour = behaviour
        self.calls = 0
        self.running = 0
        self.most_at_once = 0

    async def complete(self, **_: Any) -> ModelTurn:
        self.calls += 1
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            return await self.behaviour()
        finally:
            self.running -= 1


class Rig:
    """A worker with everything it needs, on the test's own event loop."""

    def __init__(
        self,
        db: Db,
        redis_url: str,
        model: Any,
        name: str = "w1",
        stream: str | None = None,
        **settings: Any,
    ):
        self.db = db
        self.model = model
        self.settings = Settings(
            _env_file=None,
            redis_url=SecretStr(redis_url),
            environment="test",
            retry_base_seconds=0,
            requeue_seconds=0,
            **settings,
        )
        self.redis = Redis.from_url(redis_url)
        self.stream = stream or f"test:jobs:{uuid.uuid4()}"
        self.queue = JobQueue(self.redis, stream=self.stream, consumer=name)
        self.worker = Worker(
            name,
            WorkerDeps(
                settings=self.settings,
                sessions=db.sessions,
                queue=self.queue,
                graph=build_graph(InMemorySaver()),
                model=model,
                embedder=FakeEmbedder(),
                backends=no_backends(),
            ),
        )
        db.run(self.queue.ensure_group())

    def add(self, item_ids: list[uuid.UUID]) -> None:
        self.db.run(self.queue.add([str(item_id) for item_id in item_ids]))

    def work_through_the_queue(self) -> int:
        """Handle every job now on the queue, one after another. Returns how many."""

        async def go() -> int:
            handled = 0
            while jobs := await self.queue.read(count=10, block_ms=50):
                for job in jobs:
                    await self.worker.handle(job)
                    handled += 1
            return handled

        return self.db.run(go())

    def close(self) -> None:
        self.db.run(self.redis.aclose())


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
    rig.close()


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
    rig.close()


def test_the_same_job_twice_runs_the_question_once(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())
    rig.add(started.item_ids * 2)

    handled = rig.work_through_the_queue()

    assert handled == 2
    assert rig.model.calls == 1
    assert wf.run(rig.queue.pending()) == 0
    rig.close()


def test_a_job_that_names_nothing_is_dropped(wf: Db, redis_url: str) -> None:
    rig = Rig(wf, redis_url, CountingModel())
    wf.run(rig.queue.add(["not-a-uuid", str(uuid.uuid4())]))

    assert rig.work_through_the_queue() == 2
    assert wf.run(rig.queue.pending()) == 0
    rig.close()


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
    rig.close()


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
    rig.close()


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
    rig.close()


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
    wf.run(_take(wf, item, "dead"))
    expire_lease(wf, item)

    alive = Rig(wf, redis_url, CountingModel(), name="alive", stream=dead.stream, lease_seconds=0)
    wf.run(alive.worker.sweep())
    alive.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.attempts) == ("needs_review", 2)
    assert wf.run(alive.queue.pending()) == 0
    dead.close()
    alive.close()


async def _take(db: Db, item_id: uuid.UUID, owner: str) -> None:
    from standingproof.workflow import leases

    async with db.sessions() as session:
        await leases.take_lease(session, item_id=item_id, owner=owner, seconds=60, max_attempts=3)


def _run_until(rig: Rig, done: Callable[[], Awaitable[bool]], timeout: float = 20) -> None:
    """Run the worker's own loop until `done`, then stop it the way SIGTERM would."""

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(rig.worker.run(stop))
        try:
            async with asyncio.timeout(timeout):
                # Polling: what is waited for is a row in the database, not an event here.
                while not await done():  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
        finally:
            stop.set()
            await loop

    rig.db.run(go())


def test_the_loop_finishes_a_run_without_exceeding_the_concurrency_cap(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    questions = [f"Question number {n}?" for n in range(12)]
    started = start(wf, owner, questionnaire(wf, owner, questions))

    async def slow() -> ModelTurn:
        await asyncio.sleep(0.05)
        return finishes(NO_SOURCE)

    rig = Rig(wf, redis_url, CountingModel(slow), worker_concurrency=3)
    rig.add(started.item_ids)

    async def finished() -> bool:
        async with wf.sessions() as session:
            from standingproof.models import Run

            run = await session.get(Run, started.run.id)
            return run is not None and run.status == "finished"

    _run_until(rig, finished)

    rows = items(wf, started.run.id)
    assert [row.status for row in rows] == ["needs_review"] * 12
    assert rig.model.calls == 12
    assert 1 < rig.model.most_at_once <= 3
    rig.close()


def test_on_stop_a_question_in_progress_goes_back_to_the_queue_without_losing_an_attempt(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    reached = asyncio.Event()

    async def hangs() -> ModelTurn:
        reached.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    rig = Rig(wf, redis_url, CountingModel(hangs), shutdown_grace_seconds=0.1)
    rig.add(started.item_ids)

    async def in_the_model() -> bool:
        return reached.is_set()

    _run_until(rig, in_the_model)

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason) == ("queued", "worker_stopped")
    assert row.attempts == 0
    assert row.lease_owner is None
    assert wf.run(rig.queue.pending()) == 0
    rig.close()


def test_a_bug_while_working_on_a_question_is_retried_and_never_kills_the_worker(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))

    async def broken() -> ModelTurn:
        raise RuntimeError("customer text that must not be logged")

    rig = Rig(wf, redis_url, CountingModel(broken))
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason) == ("queued", "internal_error")
    rig.close()


def test_a_job_handled_by_hand_matches_what_the_queue_delivers(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())

    wf.run(rig.worker.handle(Job(entry_id="0-1", item_id=str(started.item_ids[0]))))

    [row] = items(wf, started.run.id)
    assert row.status == "needs_review"
    rig.close()
