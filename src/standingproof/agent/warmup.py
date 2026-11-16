"""One request goes first when the provider's cache is cold.

The start of every request (tools, instruction, the reply's schema) is cached by the
provider for five minutes. Writing that cache costs 1.25 times normal input; reading it
costs a twentieth. Requests that start in the same moment each find no cache and each
write it. Measured on a five-question run: four questions, four writes of 2,024 tokens,
$0.0101 each, where one write and three reads would have cost $0.0113 in total.

So when nothing has succeeded for a while, one request goes alone and the rest wait for
it. They wait a bounded time: a first request that hangs must not use up the time the
others are allowed.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

# The provider keeps an entry for 300 seconds after its last use. Assume less.
WARM_SECONDS = 270.0
# About three typical model turns. Past this a waiter pays for its own cache write.
WAIT_SECONDS = 15.0


class FirstCallAlone:
    """Runs calls one at a time until one has succeeded, then lets them run together.

    One instance per kind of request (per cached prefix), shared by every caller in the
    process. It knows nothing about the provider: it is given a call and awaits it.
    """

    def __init__(
        self,
        *,
        warm_seconds: float = WARM_SECONDS,
        wait_seconds: float = WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._warm_seconds = warm_seconds
        self._wait_seconds = wait_seconds
        self._clock = clock
        self._warm_until = float("-inf")
        self._first = asyncio.Lock()

    def _cold(self) -> bool:
        return self._clock() >= self._warm_until

    async def _run[T](self, call: Callable[[], Awaitable[T]]) -> T:
        result = await call()
        # Only a call that came back: a failed one wrote nothing worth waiting for.
        self._warm_until = self._clock() + self._warm_seconds
        return result

    async def run[T](self, call: Callable[[], Awaitable[T]]) -> T:
        if not self._cold():
            return await self._run(call)
        try:
            async with asyncio.timeout(self._wait_seconds):
                await self._first.acquire()
        except TimeoutError:
            return await self._run(call)  # waited long enough; go without the cache
        try:
            if self._cold():
                return await self._run(call)  # this is the one that goes alone
        finally:
            self._first.release()
        return await self._run(call)  # warmed while this one waited
