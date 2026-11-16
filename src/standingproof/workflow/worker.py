"""The worker: reads jobs, takes the question's lease, runs its graph, records the result.

Any number of workers can run. What keeps them from doing a question twice is the lease
in the database, not the queue. A worker that dies leaves a lease that runs out; the sweep
of any other worker then puts the question back on the queue, and its saved state lets
the next worker continue where the dead one stopped.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError, ModelClient
from standingproof.checks.service import CheckBackends
from standingproof.checks.tools import check_tool
from standingproof.config import Settings
from standingproof.library.embedding import Embedder
from standingproof.library.tools import build_library_registry
from standingproof.workflow import leases, results, runs
from standingproof.workflow import sweep as sweeping
from standingproof.workflow.checkpoint import delete_state
from standingproof.workflow.exact import find_exact_answer
from standingproof.workflow.graph import QuestionGraph, progress, run_question
from standingproof.workflow.queue import Job, JobQueue
from standingproof.workflow.state import ExactMatch, QuestionContext

log = logging.getLogger(__name__)

REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_INTERNAL_ERROR = "internal_error"
REASON_WORKER_STOPPED = "worker_stopped"

_READ_BLOCK_MS = 1000
_SWEEP_BATCH = 200
_PRUNE_EVERY_SECONDS = 3600.0


@dataclass(frozen=True)
class WorkerDeps:
    """Everything a worker uses that lives longer than one question."""

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    queue: JobQueue
    graph: QuestionGraph
    model: ModelClient
    embedder: Embedder
    backends: CheckBackends
    # Absent in tests that keep state in memory; then nothing is pruned.
    saver: AsyncPostgresSaver | None = None
    # Tells anyone watching a run that it changed. Absent: nobody is told.
    announce: Callable[[uuid.UUID], Awaitable[None]] | None = None


class Worker:
    def __init__(self, name: str, deps: WorkerDeps) -> None:
        self.name = name
        self._deps = deps
        self._settings = deps.settings
        self._active: set[asyncio.Task[None]] = set()

    # --- one question ---

    def _context(self, lease: leases.Lease) -> QuestionContext:
        deps, settings = self._deps, self._settings
        # Tools are built per question and bound to its tenant: the model cannot name another.
        registry = build_library_registry(deps.sessions, lease.tenant_id, deps.embedder).with_tools(
            check_tool(deps.sessions, lease.tenant_id, deps.backends, settings)
        )

        async def find_exact(question: str) -> ExactMatch | None:
            async with deps.sessions() as session:
                return await find_exact_answer(
                    session, tenant_id=lease.tenant_id, question=question
                )

        return QuestionContext(
            model=deps.model,
            registry=registry,
            limits=Limits(
                max_steps=settings.run_max_steps,
                max_cost_usd=settings.run_max_cost_usd,
                max_seconds=settings.run_max_seconds,
            ),
            find_exact=find_exact,
        )

    async def _announce(self, run_id: uuid.UUID) -> None:
        if self._deps.announce is not None:
            await self._deps.announce(run_id)

    async def _keep_lease(self, lease: leases.Lease) -> None:
        """Renew the lease while the question is being worked on."""
        while True:
            await asyncio.sleep(self._settings.lease_seconds / 3)
            async with self._deps.sessions() as session:
                held = await results.renew_lease(
                    session,
                    item_id=lease.item_id,
                    owner=self.name,
                    seconds=self._settings.lease_seconds,
                )
            if not held:
                # Another worker has it now. Our result will be refused when we try to save.
                log.warning("lease lost for question %s", lease.item_id)
                return

    async def _give_back(
        self, lease: leases.Lease, reason: str, *, delay: float, refund: bool = False
    ) -> None:
        # What the question has cost so far is recorded even though it did not finish:
        # the money is spent, and the run's ceiling has to see it.
        used = await progress(self._deps.graph, str(lease.item_id))
        async with self._deps.sessions() as session:
            await results.give_back(
                session,
                progress=used,
                item_id=lease.item_id,
                owner=self.name,
                reason=reason,
                retry_in_seconds=delay,
                max_attempts=self._settings.question_max_attempts,
                refund_attempt=refund,
            )

    async def handle(self, job: Job) -> None:
        """Work on the question a job names, if it is ours to work on. Never raises."""
        try:
            await self._work(job)
        except Exception as error:
            # The database was away while taking the lease or saving. The question stays
            # open there, and the sweep will queue it again.
            log.error("job could not be handled: %s", type(error).__name__)
        finally:
            # Acknowledged whatever happened: if the question is still open, the database
            # says so and the sweep queues it again. The job itself is never the record.
            try:
                await asyncio.shield(self._deps.queue.acknowledge(job))
            except RedisError:
                log.warning("could not acknowledge a job; it will be claimed as stale")

    async def _work(self, job: Job) -> None:
        try:
            item_id = uuid.UUID(job.item_id)
        except ValueError:
            log.warning("ignored a job that does not name a question")
            return
        async with self._deps.sessions() as session:
            lease = await leases.take_lease(
                session,
                item_id=item_id,
                owner=self.name,
                seconds=self._settings.lease_seconds,
                max_attempts=self._settings.question_max_attempts,
                reserve_usd=self._settings.run_max_cost_usd,
            )
        if lease is None:
            return  # finished, cancelled, or held by another worker
        await self._announce(lease.run_id)

        keeper = asyncio.create_task(self._keep_lease(lease))
        try:
            outcome = await run_question(
                self._deps.graph,
                thread_id=str(item_id),
                question=lease.question,
                context=self._context(lease),
                resume=lease.resume,
            )
        except asyncio.CancelledError:
            # Shutting down. What was saved stays saved; the next worker continues from it.
            await asyncio.shield(
                self._give_back(lease, REASON_WORKER_STOPPED, delay=0, refund=True)
            )
            raise
        except ModelCallError:
            # A rate limit or an outage passes. Waiting is right; a person cannot fix it.
            delay = self._settings.retry_base_seconds * 2 ** (lease.attempts - 1)
            await self._give_back(lease, REASON_MODEL_UNAVAILABLE, delay=delay)
        except Exception as error:
            # The type only: an exception's message can carry customer text.
            log.error("question %s failed: %s", item_id, type(error).__name__)
            await self._give_back(
                lease, REASON_INTERNAL_ERROR, delay=self._settings.retry_base_seconds
            )
        else:
            async with self._deps.sessions() as session:
                saved = await results.save_result(
                    session, item_id=item_id, owner=self.name, outcome=outcome
                )
            if not saved:
                log.warning("result for question %s discarded: the lease was lost", item_id)
        finally:
            keeper.cancel()
            await self._announce(lease.run_id)

    # --- housekeeping ---

    async def sweep(self) -> int:
        """Queue every open question that is due, and clear jobs left by dead workers."""
        settings = self._settings
        async with self._deps.sessions() as session:
            due = await sweeping.due_for_queue(
                session, limit=_SWEEP_BATCH, requeue_seconds=settings.requeue_seconds
            )
            await sweeping.finish_idle_runs(session)
        await self._deps.queue.add([str(item_id) for item_id in due])
        # A job read long ago and never acknowledged belonged to a worker that died. Its
        # question is found through the database (above), so the job is simply removed.
        stale = await self._deps.queue.claim_stale(
            idle_ms=int(settings.lease_seconds * 1000), count=_SWEEP_BATCH
        )
        for job in stale:
            await self._deps.queue.acknowledge(job)
        return len(due)

    async def prune(self) -> int:
        """Delete saved state of questions that finished long enough ago."""
        if self._deps.saver is None:
            return 0
        async with self._deps.sessions() as session:
            await runs.expire_unreviewed(session, keep_days=self._settings.review_keep_days)
            old = await runs.finished_with_state(
                session, keep_days=self._settings.state_keep_days, limit=_SWEEP_BATCH
            )
        await delete_state(self._deps.saver, [str(item_id) for item_id in old])
        async with self._deps.sessions() as session:
            await runs.mark_state_deleted(session, old)
        return len(old)

    # --- the loop ---

    async def run(self, stop: asyncio.Event) -> None:
        """Work until `stop` is set, then let questions in progress reach a saved step."""
        settings = self._settings
        await self._deps.queue.ensure_group()
        next_sweep = next_prune = 0.0
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_sweep:
                    await self.sweep()
                    next_sweep = now + settings.sweep_seconds
                if now >= next_prune:
                    await self.prune()
                    next_prune = now + _PRUNE_EVERY_SECONDS
                free = settings.worker_concurrency - len(self._active)
                if free <= 0:
                    await asyncio.wait(self._active, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                    continue
                for job in await self._deps.queue.read(count=free, block_ms=_READ_BLOCK_MS):
                    task = asyncio.create_task(self.handle(job))
                    self._active.add(task)
                    task.add_done_callback(self._active.discard)
            except (RedisError, OSError) as error:
                # The queue or the database is away. Questions in progress carry on; wait.
                log.warning("worker loop paused: %s", type(error).__name__)
                await asyncio.sleep(1)

        if self._active:
            _, unfinished = await asyncio.wait(
                self._active, timeout=settings.shutdown_grace_seconds
            )
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
