"""The watcher: looks at settings when a delivery says so or when they are due, and
compares what it saw with the answers that rest on them.

Plain code, no model, and no model key. Every look goes through `run_check`, so the same
authorisation decides it as for any other caller. Everything here is correct when it runs
twice, late, or in two processes at once: the work is rows in Postgres, taken under locks.
"""

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.checks.service import CheckBackends, run_check, utc_now
from standingproof.config import Settings
from standingproof.db import Database
from standingproof.drift import deliveries
from standingproof.drift.examine import examine_pending
from standingproof.drift.watch import STOPPED, Setting, due, is_watched
from standingproof.limits import switch
from standingproof.models import Evidence, WebhookDelivery

log = logging.getLogger(__name__)
_FRESH = dt.timedelta(0)


@dataclass
class WatcherDeps:
    settings: Settings
    database: Database
    backends: CheckBackends
    # Tells whoever is watching a run that it changed. Best effort.
    announce: Callable[[uuid.UUID], Awaitable[None]]
    clock: Callable[[], dt.datetime] = utc_now


class Watcher:
    def __init__(self, deps: WatcherDeps) -> None:
        self._deps = deps
        self._settings = deps.settings

    async def _stopped(self, tenant_id: uuid.UUID) -> bool:
        async with self._deps.database.tenant(tenant_id)() as session:
            return (await switch.read(session, tenant_id, self._settings)).stopped

    async def _look(self, setting: Setting, trigger: str) -> Evidence | None:
        """Look at a setting. None when the stop switch refused the look."""
        # As the tenant whose setting it is: what is seen is that tenant's to keep.
        async with self._deps.database.tenant(setting.tenant_id)() as session:
            seen = await run_check(
                session,
                tenant_id=setting.tenant_id,
                name=setting.check_name,
                scope=setting.scope,
                backends=self._deps.backends,
                settings=self._settings,
                # Never an observation from before: the point is to see the setting now.
                max_age=_FRESH,
                trigger=trigger,
            )
            return None if seen.reason == STOPPED else seen

    async def _last_look(
        self, session: AsyncSession, setting: Setting
    ) -> tuple[int, dt.datetime] | None:
        """The number and the time of the last look at this setting, whatever came of it."""
        found = (
            await session.execute(
                select(Evidence.seq, Evidence.observed_at)
                .where(
                    Evidence.tenant_id == setting.tenant_id,
                    Evidence.check_name == setting.check_name,
                    Evidence.scope == setting.scope,
                    Evidence.reason.is_distinct_from(STOPPED),
                )
                .order_by(Evidence.seq.desc())
                .limit(1)
            )
        ).one_or_none()
        return None if found is None else (found.seq, found.observed_at)

    async def _answer_deliveries(self, now: dt.datetime) -> None:
        settle = dt.timedelta(seconds=self._settings.webhook_settle_seconds)
        gap = dt.timedelta(seconds=self._settings.webhook_min_gap_seconds)
        async with self._deps.database.across_tenants() as session:
            # Locked until this session commits; another watcher passes them over.
            waiting = await deliveries.waiting(
                session, before=now - settle, limit=self._settings.drift_batch
            )
            by_setting: dict[Setting, list[WebhookDelivery]] = {}
            for delivery in waiting:
                if delivery.tenant_id is None or not delivery.check_name or not delivery.scope:
                    continue
                setting = Setting(delivery.tenant_id, delivery.check_name, delivery.scope)
                by_setting.setdefault(setting, []).append(delivery)
            stopped: dict[uuid.UUID, bool] = {}
            for setting, group in by_setting.items():
                if setting.tenant_id not in stopped:
                    stopped[setting.tenant_id] = await self._stopped(setting.tenant_id)
                if stopped[setting.tenant_id]:
                    # Nothing is looked at while stopped. The deliveries stay waiting, so
                    # the change they announce is read as soon as work resumes.
                    continue
                outcome = "looked"
                # Which waiting deliveries this decision covers: those stored before the
                # look numbered `before_seq` (None: all), received no later than `settled_by`.
                before_seq: int | None = None
                settled_by: dt.datetime | None = None
                if not await is_watched(session, setting):
                    # No answer rests on it, so there is nothing a look could reopen. A
                    # sender who names repository after repository gets no look for any.
                    outcome = "ignored:not_watched"
                else:
                    last = await self._last_look(session, setting)
                    newest = max(delivery.seq for delivery in group)
                    # A look stored after the newest delivery has already answered them,
                    # and every other delivery stored before that look. "After" is by the
                    # number both take from one sequence, never by two clocks: a delivery
                    # stamped a second early would otherwise count as answered unseen.
                    if last is not None and last[0] > newest:
                        before_seq = last[0]
                    else:
                        if last is not None and last[1] > now - gap:
                            # Too soon after the last look. They stay waiting, so the last
                            # delivery of a burst still gets its look when the gap has passed.
                            continue
                        seen = await self._look(setting, "webhook")
                        if seen is None:
                            # Stopped between our reading the switch and the look. The
                            # deliveries stay waiting; nothing was read for them.
                            continue
                        # The look answers what was stored before it and had settled when
                        # this pass began. A delivery that came later may be about a later
                        # change.
                        before_seq, settled_by = seen.seq, now - settle
                for delivery in group:
                    delivery.handled_at = now
                    delivery.outcome = outcome
                await session.flush()
                # And the ones beyond this pass's batch: the same look answers them.
                await deliveries.mark_handled(
                    session,
                    tenant_id=setting.tenant_id,
                    check_name=setting.check_name,
                    scope=setting.scope,
                    before_seq=before_seq,
                    settled_by=settled_by,
                    outcome=outcome,
                    now=now,
                )
            await session.commit()
        async with self._deps.database.across_tenants() as session:
            keep = dt.timedelta(days=self._settings.webhook_keep_days)
            await deliveries.prune(session, before=now - keep)

    async def _look_at_due(self, now: dt.datetime) -> None:
        async with self._deps.database.across_tenants() as session:
            settings = await due(
                session,
                now=now,
                recheck=dt.timedelta(seconds=self._settings.recheck_seconds),
                retry=dt.timedelta(seconds=self._settings.recheck_retry_seconds),
                limit=self._settings.drift_batch,
            )
        stopped: dict[uuid.UUID, bool] = {}
        for setting in settings:
            if setting.tenant_id not in stopped:
                stopped[setting.tenant_id] = await self._stopped(setting.tenant_id)
            if not stopped[setting.tenant_id]:
                await self._look(setting, "schedule")

    async def tick(self) -> None:
        """One pass: answer deliveries, look at what is due, compare what was seen."""
        now = self._deps.clock()
        await self._answer_deliveries(now)
        await self._look_at_due(now)
        runs = await examine_pending(self._deps.database, limit=self._settings.drift_batch)
        for run_id in runs:
            await self._deps.announce(run_id)

    async def run(self, stop: asyncio.Event) -> None:
        """Pass after pass until told to stop. A pass that fails is tried again."""
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as error:  # noqa: BLE001 - the loop must outlive any one pass
                # The type only: a message can carry text from a customer or a system.
                log.error("watcher pass failed: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.drift_poll_seconds)
            except TimeoutError:
                continue
