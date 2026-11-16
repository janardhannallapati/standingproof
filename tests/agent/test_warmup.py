"""One request goes first when the provider's cache is cold. No network, no real clock."""

import asyncio

import pytest

from standingproof.agent.warmup import FirstCallAlone


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Calls:
    """Calls that stay in progress until each is let go, in the order they started."""

    def __init__(self) -> None:
        self.holds: list[asyncio.Event] = []
        self.running = 0
        self.fail_first = False

    @property
    def started(self) -> int:
        return len(self.holds)

    async def __call__(self) -> str:
        hold = asyncio.Event()
        fail = self.fail_first and not self.holds
        self.holds.append(hold)
        self.running += 1
        try:
            await hold.wait()
            if fail:
                raise RuntimeError("the provider said no")
            return "reply"
        finally:
            self.running -= 1

    def let_go(self, *which: int) -> None:
        for index in which or range(len(self.holds)):
            self.holds[index].set()


async def settle() -> None:
    """Let every task run as far as it can without a clock or a call being let go."""
    for _ in range(50):
        await asyncio.sleep(0)


def start(gate: FirstCallAlone, calls: Calls, count: int) -> list[asyncio.Task[str]]:
    return [asyncio.create_task(gate.run(calls)) for _ in range(count)]


async def test_when_the_cache_is_cold_one_call_goes_first() -> None:
    gate, calls = FirstCallAlone(clock=Clock()), Calls()

    tasks = start(gate, calls, 4)
    await settle()

    assert (calls.started, calls.running) == (1, 1)  # three are waiting for the first
    calls.let_go(0)
    await settle()
    calls.let_go()
    assert await asyncio.gather(*tasks) == ["reply"] * 4


async def test_the_calls_that_waited_then_run_at_the_same_time() -> None:
    gate, calls = FirstCallAlone(clock=Clock()), Calls()
    tasks = start(gate, calls, 4)
    await settle()

    calls.let_go(0)
    await settle()

    assert (calls.started, calls.running) == (4, 3)
    calls.let_go()
    await asyncio.gather(*tasks)


async def test_once_a_call_has_succeeded_nobody_waits() -> None:
    gate, calls = FirstCallAlone(clock=Clock()), Calls()
    [first] = start(gate, calls, 1)
    await settle()
    calls.let_go()
    await first

    tasks = start(gate, calls, 3)
    await settle()

    assert calls.running == 3
    calls.let_go()
    await asyncio.gather(*tasks)


async def test_after_the_cache_has_expired_one_call_goes_first_again() -> None:
    clock = Clock()
    gate, calls = FirstCallAlone(warm_seconds=270, clock=clock), Calls()
    [first] = start(gate, calls, 1)
    await settle()
    calls.let_go()
    await first
    clock.now += 271

    tasks = start(gate, calls, 3)
    await settle()

    assert calls.running == 1
    calls.let_go()
    await settle()
    calls.let_go()
    await asyncio.gather(*tasks)


async def test_a_first_call_that_fails_warms_nothing_so_the_next_one_goes_alone() -> None:
    gate, calls = FirstCallAlone(clock=Clock()), Calls()
    calls.fail_first = True
    tasks = start(gate, calls, 3)
    await settle()

    calls.let_go(0)
    await settle()

    assert (calls.started, calls.running) == (2, 1)  # the second goes alone, the third waits
    with pytest.raises(RuntimeError):
        await tasks[0]
    calls.let_go()
    await settle()
    calls.let_go()
    assert await asyncio.gather(*tasks[1:]) == ["reply", "reply"]


async def test_nobody_waits_longer_than_the_limit_for_a_first_call_that_hangs() -> None:
    gate, calls = FirstCallAlone(wait_seconds=0.05, clock=Clock()), Calls()
    tasks = start(gate, calls, 2)
    await settle()
    assert calls.running == 1

    await asyncio.sleep(0.2)

    # The first call is still in progress, and the second has stopped waiting for it.
    assert calls.running == 2
    calls.let_go()
    assert await asyncio.gather(*tasks) == ["reply", "reply"]


async def test_a_cancelled_first_call_lets_the_next_one_through() -> None:
    gate, calls = FirstCallAlone(clock=Clock()), Calls()
    first, second = start(gate, calls, 2)
    await settle()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await settle()

    assert calls.running == 1
    calls.let_go()
    assert await second == "reply"
