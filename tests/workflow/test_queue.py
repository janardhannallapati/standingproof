"""The job queue against a real Redis: delivery, acknowledgement, and a dead reader."""

import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from standingproof.workflow.queue import JobQueue


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


@pytest.fixture
def stream() -> str:
    # A stream of its own per test, so tests cannot read each other's jobs.
    return f"test:jobs:{uuid.uuid4()}"


def queue(redis: Redis, stream: str, consumer: str) -> JobQueue:
    return JobQueue(redis, stream=stream, consumer=consumer)


async def test_a_job_is_delivered_to_one_reader_of_the_group(redis: Redis, stream: str) -> None:
    first, second = queue(redis, stream, "w1"), queue(redis, stream, "w2")
    await first.ensure_group()
    await first.add(["a", "b"])

    got_first = await first.read(count=1, block_ms=100)
    got_second = await second.read(count=5, block_ms=100)
    nothing_left = await first.read(count=5, block_ms=100)

    assert [job.item_id for job in got_first] == ["a"]
    assert [job.item_id for job in got_second] == ["b"]
    assert nothing_left == []


async def test_creating_the_group_twice_is_harmless(redis: Redis, stream: str) -> None:
    jobs = queue(redis, stream, "w1")

    await jobs.ensure_group()
    await jobs.ensure_group()

    assert await jobs.pending() == 0


async def test_jobs_added_before_the_group_existed_are_still_delivered(
    redis: Redis, stream: str
) -> None:
    jobs = queue(redis, stream, "w1")
    await jobs.add(["early"])

    await jobs.ensure_group()

    assert [job.item_id for job in await jobs.read(count=5, block_ms=100)] == ["early"]


async def test_an_acknowledged_job_is_gone(redis: Redis, stream: str) -> None:
    jobs = queue(redis, stream, "w1")
    await jobs.ensure_group()
    await jobs.add(["a"])
    [job] = await jobs.read(count=1, block_ms=100)
    assert await jobs.pending() == 1

    await jobs.acknowledge(job)

    assert await jobs.pending() == 0
    assert await redis.xlen(stream) == 0
    assert await queue(redis, stream, "w2").claim_stale(idle_ms=0, count=5) == []


async def test_a_job_read_and_never_acknowledged_is_claimed_by_another_reader(
    redis: Redis, stream: str
) -> None:
    dead, alive = queue(redis, stream, "dead"), queue(redis, stream, "alive")
    await dead.ensure_group()
    await dead.add(["a"])
    await dead.read(count=1, block_ms=100)

    too_soon = await alive.claim_stale(idle_ms=60_000, count=5)
    claimed = await alive.claim_stale(idle_ms=0, count=5)

    assert too_soon == []
    assert [job.item_id for job in claimed] == ["a"]


async def test_adding_nothing_does_nothing(redis: Redis, stream: str) -> None:
    jobs = queue(redis, stream, "w1")

    await jobs.add([])

    assert await redis.exists(stream) == 0
