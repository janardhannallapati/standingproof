"""The worker's loop, stopping, failures and redrafts, on real Postgres and Redis."""

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import ModelTurn
from standingproof.workflow.queue import Job
from tests.agent.fakes import finishes
from tests.conftest import Db
from tests.workflow.rig import NO_SOURCE, CountingModel, Rig
from tests.workflow.seed import (
    approved_answer,
    items,
    questionnaire,
    start,
    tenant,
    wf,
)

__all__ = ["wf"]


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


def test_a_job_handled_by_hand_matches_what_the_queue_delivers(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())

    wf.run(rig.worker.handle(Job(entry_id="0-1", item_id=str(started.item_ids[0]))))

    [row] = items(wf, started.run.id)
    assert row.status == "needs_review"


def test_what_a_failed_attempt_cost_is_recorded_on_the_question(wf: Db, redis_url: str) -> None:
    from tests.agent.fakes import search, wants_tools

    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    turns = iter([wants_tools(search("c1", "backups"))])

    async def one_turn_then_rate_limited() -> ModelTurn:
        try:
            return next(turns)
        except StopIteration:
            raise ModelCallError("rate limited", retryable=True) from None

    rig = Rig(wf, redis_url, CountingModel(one_turn_then_rate_limited))
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason, row.steps) == ("queued", "model_unavailable", 1)
    assert row.cost_usd > 0


# --- A reviewer sends a question back, and a worker redrafts it. ---


def test_a_redraft_is_made_by_the_worker_with_the_reviewers_instruction_and_announced(
    wf: Db, redis_url: str
) -> None:
    from standingproof.workflow import review

    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes, for all staff.")
    started = start(wf, owner, questionnaire(wf, owner, ["Is MFA enforced?"]))
    seen: list[dict[str, Any]] = []
    announced: list[uuid.UUID] = []

    class Recording(CountingModel):
        async def complete(self, **request: Any) -> ModelTurn:
            seen.append(request)
            return await super().complete(**request)

    async def announce(run_id: uuid.UUID) -> None:
        announced.append(run_id)

    rig = Rig(wf, redis_url, Recording())
    rig.worker._deps = dataclasses.replace(rig.worker._deps, announce=announce)
    rig.add(started.item_ids)
    rig.work_through_the_queue()
    [row] = items(wf, started.run.id)
    assert (row.path, row.review, row.cost_usd) == ("reused", "pending", 0)
    assert seen == []

    async def send_back() -> None:
        async with wf.sessions() as session:
            await review.decide(
                session,
                tenant_id=owner,
                item_id=row.id,
                decision="redraft",
                version=row.version,
                reviewer="asha",
                comment="Say that contractors are included.",
                max_redrafts=2,
                max_chars=4000,
            )

    wf.run(send_back())
    rig.add([row.id])
    rig.work_through_the_queue()

    [after] = items(wf, started.run.id)
    # The scripted model answers "no approved source"; what matters is the path it took.
    assert (after.status, after.review, after.redrafts) == ("needs_review", "pending", 1)
    assert after.version > row.version
    assert after.cost_usd > 0
    [request] = seen
    assert "contractors are included" in request["system_addendum"]
    assert "contractors" not in str(request["messages"])
    assert announced.count(started.run.id) >= 4  # taken and saved, twice
