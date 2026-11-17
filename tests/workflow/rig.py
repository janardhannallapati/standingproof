"""A worker with a scripted model on real Postgres and Redis, for the worker tests."""

import sys
import traceback
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from redis.asyncio import Redis

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import AnswerDraft, ModelTurn
from standingproof.config import Settings
from standingproof.limits.guard import Guard
from standingproof.workflow.graph import build_graph
from standingproof.workflow.queue import Job, JobQueue
from standingproof.workflow.worker import Worker, WorkerDeps
from tests.agent.fakes import finishes
from tests.conftest import Db, no_backends
from tests.fakes import FakeEmbedder

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
        checker: Any = None,
        backends: Any = None,
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
        # Closed with the test's database, whether the test passed or not: a client left
        # open is reported as a warning during whichever test runs next, and fails that one.
        db.closing(self.redis.aclose)
        self.stream = stream or f"test:jobs:{uuid.uuid4()}"
        self.queue = JobQueue(self.redis, stream=self.stream, consumer=name)
        self.worker = Worker(
            name,
            WorkerDeps(
                settings=self.settings,
                database=db.database,
                queue=self.queue,
                graph=build_graph(InMemorySaver()),
                model=model,
                checker=checker,
                embedder=FakeEmbedder(),
                backends=backends or no_backends(),
                guard=Guard(self.redis, db.database, self.settings),
            ),
        )
        db.run(self.queue.ensure_group())
        # The worker never raises from a job: a question that fails is given back to the
        # queue and only the error's type is logged. A test that did not expect that then
        # fails on some later assertion with no cause in sight, so the cause is kept here.
        self.given_back: list[tuple[str, str]] = []
        give_back = self.worker._give_back

        async def recording(lease: Any, reason: str, **how: Any) -> None:
            active = sys.exc_info()
            trace = "".join(traceback.format_exception(*active)) if active[0] else ""
            self.given_back.append((reason, trace))
            await give_back(lease, reason, **how)

        self.worker._give_back = recording  # type: ignore[method-assign]

        # The other way a cause is lost: an error outside the question itself (taking the
        # lease, saving the result, giving back) is caught by `handle`, which logs its type.
        work = self.worker._work

        async def watched(job: Job) -> None:
            try:
                await work(job)
            except Exception:
                self.given_back.append(("not_handled", traceback.format_exc()))
                raise

        self.worker._work = watched  # type: ignore[method-assign]

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


async def take(db: Db, item_id: uuid.UUID, owner: str) -> None:
    from standingproof.workflow import leases

    async with db.sessions() as session:
        await leases.take_lease(
            session, item_id=item_id, owner=owner, seconds=60, max_attempts=3, reserve_usd=0.5
        )
