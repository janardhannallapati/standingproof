"""The watcher process.

    python -m standingproof.drift

Looks at the live settings that answers rest on (when GitHub says one changed, and once a
day otherwise) and puts an answer back in front of a reviewer when its setting reads
differently. Calls GitHub and AWS read-only. Never calls a model and needs no model key.
"""

import asyncio
import logging
import signal

from redis.asyncio import Redis

from standingproof.checks.service import build_backends
from standingproof.config import Settings, get_settings
from standingproof.db import build_database
from standingproof.drift.watcher import Watcher, WatcherDeps
from standingproof.workflow.events import publish_changed

log = logging.getLogger(__name__)


async def serve(settings: Settings, *, stop: asyncio.Event | None = None) -> None:
    """Run one watcher. Returns after SIGTERM or SIGINT, when the pass in progress ends."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    database = build_database(settings)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=5
    )
    backends = build_backends(settings)
    try:
        watcher = Watcher(
            WatcherDeps(
                settings=settings,
                database=database,
                backends=backends,
                announce=lambda run_id: publish_changed(redis, run_id),
            )
        )
        log.info("watcher started")
        await watcher.run(stop)
        log.info("watcher stopped")
    finally:
        await backends.aclose()
        await redis.aclose()
        await database.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(get_settings()))


if __name__ == "__main__":
    main()
