"""The job queue: a Redis stream read by a group of workers.

A job is the id of one question in a run and nothing else; no customer text goes through
Redis. Delivery is at least once: a job can arrive twice, and after a Redis restart it may
not arrive at all. The database decides who works on what (`runs.take_lease`), and the
sweep puts back what went missing, so neither case loses or repeats work.
"""

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

GROUP = "workers"
_FIELD = "item"


@dataclass(frozen=True)
class Job:
    entry_id: str
    item_id: str


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class JobQueue:
    def __init__(self, redis: Redis, *, stream: str, consumer: str, max_length: int = 100_000):
        self._redis = redis
        self._stream = stream
        self._consumer = consumer
        self._max_length = max_length

    async def ensure_group(self) -> None:
        """Create the stream and the workers' group if they are not there. Safe to repeat."""
        try:
            # "0": a group created late still sees jobs that were added before it existed.
            await self._redis.xgroup_create(self._stream, GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def add(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        pipeline = self._redis.pipeline(transaction=False)
        for item_id in item_ids:
            # The cap is a safety net; acknowledged jobs are deleted as they finish.
            pipeline.xadd(
                self._stream, {_FIELD: item_id}, maxlen=self._max_length, approximate=True
            )
        await pipeline.execute()

    def _jobs(self, entries: Any) -> list[Job]:
        jobs: list[Job] = []
        for entry_id, fields in entries:
            # A claimed entry that was deleted meanwhile comes back with no fields.
            values = {_text(key): _text(value) for key, value in (fields or {}).items()}
            if _FIELD in values:
                jobs.append(Job(entry_id=_text(entry_id), item_id=values[_FIELD]))
        return jobs

    async def read(self, *, count: int, block_ms: int) -> list[Job]:
        """New jobs for this worker. Each stays pending until acknowledged."""
        # The client types this reply for every protocol version at once; we read one shape.
        reply: Any = await self._redis.xreadgroup(
            GROUP, self._consumer, {self._stream: ">"}, count=count, block=block_ms
        )
        return self._jobs(reply[0][1]) if reply else []

    async def claim_stale(self, *, idle_ms: int, count: int) -> list[Job]:
        """Jobs another worker read and has not acknowledged for `idle_ms`: it has died."""
        reply: Any = await self._redis.xautoclaim(
            self._stream, GROUP, self._consumer, min_idle_time=idle_ms, start_id="0-0", count=count
        )
        return self._jobs(reply[1])

    async def acknowledge(self, job: Job) -> None:
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.xack(self._stream, GROUP, job.entry_id)
        pipeline.xdel(self._stream, job.entry_id)
        await pipeline.execute()

    async def pending(self) -> int:
        """How many jobs have been read and not acknowledged, across all workers."""
        summary = await self._redis.xpending(self._stream, GROUP)
        return int(summary["pending"])
