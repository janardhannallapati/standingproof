"""Kill a worker process in the middle of a run; another one finishes it.

Real processes, a real SIGKILL, real Postgres and Redis. Marked slow: it waits for leases
to run out.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest

from standingproof.workflow.checkpoint import setup
from standingproof.workflow.queue import JobQueue
from tests.conftest import Db
from tests.workflow.seed import items, questionnaire, start, tenant, wf

__all__ = ["wf"]

QUESTIONS = [f"Question number {n}?" for n in range(8)]


def spawn(env: dict[str, str], calls: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - our own module, fixed arguments
        [sys.executable, "-m", "tests.workflow.fake_worker", str(calls)],
        env=env,
        cwd=Path(__file__).parents[2],
    )


def wait_for(condition: object, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


@pytest.mark.slow
def test_a_killed_worker_loses_nothing_and_no_question_is_answered_twice(
    wf: Db, migrated_postgres_url: str, redis_url: str, tmp_path: Path
) -> None:
    asyncio.run(setup(migrated_postgres_url))
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, QUESTIONS))
    stream = f"test:jobs:{uuid.uuid4()}"

    async def enqueue() -> None:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url)
        try:
            queue = JobQueue(redis, stream=stream, consumer="test")
            await queue.ensure_group()
            await queue.add([str(item_id) for item_id in started.item_ids])
        finally:
            await redis.aclose()

    wf.run(enqueue())
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {
        **os.environ,
        "SP_DATABASE_URL": migrated_postgres_url,
        "SP_REDIS_URL": redis_url,
        "SP_ENVIRONMENT": "test",
        "SP_QUEUE_STREAM": stream,
        "SP_WORKER_CONCURRENCY": "2",
        # Short, so the second worker takes over within the test's patience.
        "SP_LEASE_SECONDS": "3",
        "SP_SWEEP_SECONDS": "0.5",
        "SP_REQUEUE_SECONDS": "1",
    }

    def lines() -> list[str]:
        return calls.read_text().splitlines()

    first = spawn(env, calls)
    try:
        wait_for(lambda: len(lines()) >= 3, 60, "the first worker to make three model calls")
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        before_kill = len(lines())

        second = spawn(env, calls)
        try:
            wait_for(
                lambda: all(row.finished_at is not None for row in items(wf, started.run.id)),
                90,
                "the second worker to finish the run",
            )
            second.send_signal(signal.SIGTERM)
            assert second.wait(timeout=30) == 0  # a clean exit on SIGTERM
        finally:
            second.kill()
    finally:
        first.kill()

    rows = items(wf, started.run.id)
    assert [row.status for row in rows] == ["needs_review"] * len(QUESTIONS)
    assert all(row.steps == 2 for row in rows)
    # At least one question was in the first worker's hands when it died.
    assert any(row.attempts == 2 for row in rows)

    per_step = Counter(lines())
    assert len(per_step) == 2 * len(QUESTIONS)  # every step of every question happened
    # A step is repeated only if the kill landed in the middle of it: at most the two
    # questions in progress. Everything saved before the kill was not paid for again.
    repeated = sum(count - 1 for count in per_step.values())
    assert repeated <= 2
    assert before_kill < 2 * len(QUESTIONS)
