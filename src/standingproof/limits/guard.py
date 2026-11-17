"""What stands in front of every model call: the stop switch, then the day's ceiling.

The ceiling is counted in Redis, in millionths of a dollar. A call first reserves the most
it is expected to cost; when it returns, the reservation is replaced by what it did cost.
"Check, then spend" would let twenty calls starting together each see room for one.
Reserving is one script, so of twenty, only as many pass as there is room for.

Fail closed: if the switch cannot be read or the counter cannot be reached, no call is made.
"""

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from standingproof.agent.loop import ModelCallError, ModelClient
from standingproof.agent.pricing import cost_usd
from standingproof.agent.types import CheckTurn, HandoffReason, ModelTurn
from standingproof.config import Settings
from standingproof.db import Database
from standingproof.limits import switch

MICRO = 1_000_000
_KEEP_SECONDS = 3 * 24 * 60 * 60
# KEYS: the tenant's counter, the deployment's. ARGV: reserve, tenant ceiling, deployment
# ceiling, seconds to keep. Returns 0 (reserved), 1 (tenant ceiling) or 2 (deployment's).
_RESERVE = """
local reserve = tonumber(ARGV[1])
if tonumber(redis.call('GET', KEYS[1]) or '0') + reserve > tonumber(ARGV[2]) then return 1 end
if tonumber(redis.call('GET', KEYS[2]) or '0') + reserve > tonumber(ARGV[3]) then return 2 end
redis.call('INCRBY', KEYS[1], reserve)
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('INCRBY', KEYS[2], reserve)
redis.call('EXPIRE', KEYS[2], ARGV[4])
return 0
"""


class CallRefusedError(ModelCallError):
    """A model call that was not made, by our own rule. Not retryable: waiting a few
    seconds changes nothing, so the question goes to a person with the reason."""

    def __init__(self, reason: HandoffReason) -> None:
        super().__init__(f"not called: {reason.value}", retryable=False)
        self.reason = reason


def _micro(usd: Decimal | float) -> int:
    return int(Decimal(str(usd)) * MICRO)


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


@dataclass(frozen=True)
class Reservation:
    keys: tuple[str, str]
    micro: int


class Guard:
    def __init__(
        self,
        redis: Redis,
        database: Database,
        settings: Settings,
        *,
        today: Callable[[], dt.date] = utc_today,
    ) -> None:
        self._redis = redis
        self._database = database
        self._settings = settings
        self._today = today

    def keys(self, tenant_id: uuid.UUID) -> tuple[str, str]:
        day = self._today().isoformat()
        return (f"standingproof:spend:{day}:{tenant_id}", f"standingproof:spend:{day}:all")

    async def reserve(self, tenant_id: uuid.UUID) -> Reservation:
        """Room for one model call, or the reason there is none."""
        async with self._database.tenant(tenant_id)() as session:
            state = await switch.read(session, tenant_id, self._settings)
        if state.stopped:
            raise CallRefusedError(HandoffReason.STOPPED)
        keys = self.keys(tenant_id)
        micro = _micro(self._settings.call_reserve_usd)
        try:
            refused = await self._redis.eval(
                _RESERVE,
                2,
                *keys,
                micro,
                _micro(state.tenant_daily_usd),
                _micro(state.deployment_daily_usd),
                _KEEP_SECONDS,
            )
        except (RedisError, OSError) as error:
            # Our own counter is away. That passes, so the question is tried again later;
            # what it must not do is go ahead uncounted.
            raise ModelCallError("the spend counter cannot be reached", retryable=True) from error
        if refused:
            raise CallRefusedError(HandoffReason.DAILY_LIMIT)
        return Reservation(keys, micro)

    async def settle(self, reservation: Reservation, actual_usd: float) -> None:
        """Replace what was set aside with what the call cost (nothing, if it failed)."""
        difference = _micro(actual_usd) - reservation.micro
        if difference == 0:
            return
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key in reservation.keys:
                    pipe.incrby(key, difference)
                await pipe.execute()
        except (RedisError, OSError):
            # The reservation stays counted. Erring towards "more was spent" is the safe side.
            return

    async def spent_today(self, tenant_id: uuid.UUID) -> tuple[Decimal, Decimal]:
        """What the tenant and the deployment have spent and reserved today, in dollars."""
        values = await self._redis.mget(self.keys(tenant_id))
        tenant, everyone = (Decimal(int(value or 0)) / MICRO for value in values)
        return tenant, everyone


class GuardedModel:
    """A model client for one tenant: every call is reserved first and settled after."""

    def __init__(self, inner: ModelClient, guard: Guard, tenant_id: uuid.UUID) -> None:
        self._inner, self._guard, self._tenant_id = inner, guard, tenant_id

    async def complete(self, **request: Any) -> ModelTurn:
        reservation = await self._guard.reserve(self._tenant_id)
        cost = 0.0
        try:
            turn = await self._inner.complete(**request)
            cost = cost_usd(turn.model, turn.usage)
            return turn
        finally:
            await self._guard.settle(reservation, cost)


class GuardedChecker:
    """The same, for the second reader of a draft."""

    def __init__(self, inner: Any, guard: Guard, tenant_id: uuid.UUID) -> None:
        self._inner, self._guard, self._tenant_id = inner, guard, tenant_id

    async def check(self, **request: Any) -> CheckTurn:
        reservation = await self._guard.reserve(self._tenant_id)
        cost = 0.0
        try:
            turn: CheckTurn = await self._inner.check(**request)
            cost = cost_usd(turn.model, turn.usage)
            return turn
        finally:
            await self._guard.settle(reservation, cost)
