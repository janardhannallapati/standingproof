"""Build a worker from the settings and run it until the process is told to stop."""

import asyncio
import logging
import os
import signal
import socket
import uuid

from redis.asyncio import Redis

from standingproof.agent.checker import CheckerClient
from standingproof.agent.loop import ModelClient
from standingproof.checks.service import build_backends
from standingproof.config import Settings
from standingproof.db import build_engine, build_session_factory
from standingproof.library.embedding import Embedder, build_embedder
from standingproof.workflow.checkpoint import open_checkpointer
from standingproof.workflow.events import publish_changed
from standingproof.workflow.graph import build_graph
from standingproof.workflow.queue import JobQueue
from standingproof.workflow.worker import Worker, WorkerDeps

log = logging.getLogger(__name__)


def worker_name() -> str:
    """Unique per process, so a restarted worker never mistakes an old lease for its own."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def serve(
    settings: Settings,
    model: ModelClient,
    *,
    checker: CheckerClient | None = None,
    embedder: Embedder | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Run one worker. Returns after SIGTERM or SIGINT, once work in progress is saved."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    database_url = settings.database_url.get_secret_value()
    engine = build_engine(database_url)
    # A read blocks for a second waiting for a job, so the socket must wait longer than that.
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=5
    )
    embedder = embedder or build_embedder(settings)
    backends = build_backends(settings)
    name = worker_name()
    try:
        # One connection per question in progress, and one for housekeeping.
        async with open_checkpointer(
            database_url, max_connections=settings.worker_concurrency + 1
        ) as saver:
            worker = Worker(
                name,
                WorkerDeps(
                    settings=settings,
                    sessions=build_session_factory(engine),
                    queue=JobQueue(redis, stream=settings.queue_stream, consumer=name),
                    graph=build_graph(saver),
                    model=model,
                    checker=checker,
                    embedder=embedder,
                    backends=backends,
                    saver=saver,
                    announce=lambda run_id: publish_changed(redis, run_id),
                ),
            )
            log.info("worker %s started", name)
            await worker.run(stop)
            log.info("worker %s stopped", name)
    finally:
        await backends.aclose()
        await embedder.aclose()
        await redis.aclose()
        await engine.dispose()
