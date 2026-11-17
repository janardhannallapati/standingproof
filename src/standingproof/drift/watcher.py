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

    async def _look(self, setting: Setting, trigger: str) -> bool:
        """Look at a setting. False when the stop switch refused the look."""
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
            return seen.reason != STOPPED

    async def _last_look(self, session: AsyncSession, setting: Setting) -> dt.datetime | None:
        """When this setting was last looked at, whatever came of it."""
        return (
            await session.execute(
                select(Evidence.observed_at)
                .where(
                    Evidence.tenant_id == setting.tenant_id,
                    Evidence.check_name == setting.check_name,
                    Evidence.scope == setting.scope,
                    Evidence.reason.is_distinct_from(STOPPED),
                )
                .order_by(Evidence.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

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
                # Up to when waiting deliveries count as dealt with by this decision.
                upto = now
                if not await is_watched(session, setting):
                    # No answer rests on it, so there is nothing a look could reopen. A
                    # sender who names repository after repository gets no look for any.
                    outcome = "ignored:not_watched"
                else:
                    last = await self._last_look(session, setting)
                    newest = max(delivery.received_at for delivery in group)
                    # A look made after the newest delivery arrived has already answered
                    # them, and every other delivery that arrived before that look.
                    upto = last if last is not None else now
                    if last is None or last < newest:
                        if last is not None and last > now - gap:
                            # Too soon after the last look. They stay waiting, so the last
                            # delivery of a burst still gets its look when the gap has passed.
                            continue
                        if not await self._look(setting, "webhook"):
                            # Stopped between our reading the switch and the look. The
                            # deliveries stay waiting; nothing was read for them.
                            continue
                        # The look answers what had settled when this pass began. A
                        # delivery that came later may be about a later change.
                        upto = now - settle
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
                    upto=upto,
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
