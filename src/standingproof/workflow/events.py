"""Tell whoever is watching a run that something about it changed.

The message says only that: no question, no answer, no count. A watcher then asks the API
for the current state, where the tenant check is. So a message can be lost, repeated or
arrive out of order without anything being wrong, and nothing private travels on it.
"""

import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

CHANGED = "changed"


def channel(run_id: uuid.UUID) -> str:
    return f"standingproof:run:{run_id}"


async def publish_changed(redis: Redis, run_id: uuid.UUID) -> None:
    """Best effort. A watcher that misses this still finds out on its next look."""
    try:
        await redis.publish(channel(run_id), CHANGED)
    except (RedisError, OSError) as error:
        log.warning("change to run %s not announced: %s", run_id, type(error).__name__)
