"""Webhook deliveries: kept once, then worked through by the watcher.

Postgres is the record of what there is to do. A delivery that arrives twice is one row; a
delivery that arrives while the watcher is down waits here.
"""

import datetime as dt
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.drift.github_events import Reading
from standingproof.models import Connection, WebhookDelivery

GITHUB = "github"


async def store(
    session: AsyncSession, *, delivery_id: str, event: str, reading: Reading, now: dt.datetime
) -> bool:
    """Keep one GitHub delivery. False when this delivery id was kept before. Commits."""
    tenant_id: uuid.UUID | None = None
    ignored = reading.ignored
    if reading.org is not None:
        # Whoever connected the organisation is who the delivery is for. A scope is held
        # by one tenant (uq_connections_scope), so there is at most one.
        tenant_id = (
            await session.execute(
                select(Connection.tenant_id).where(
                    Connection.system == GITHUB, Connection.scope == reading.org
                )
            )
        ).scalar_one_or_none()
    if ignored is None and tenant_id is None:
        ignored = "not_connected"
    look = reading.looks[0] if reading.looks and ignored is None else None
    made = (
        await session.execute(
            insert(WebhookDelivery)
            .values(
                source=GITHUB,
                delivery_id=delivery_id,
                event=event,
                action=reading.action,
                tenant_id=tenant_id,
                check_name=look.check if look else None,
                scope=look.scope if look else None,
                received_at=now,
                # Nothing to do for an ignored delivery: it is kept as a record only.
                handled_at=now if look is None else None,
                outcome=f"ignored:{ignored}" if look is None else None,
            )
            # A redelivery, a retry or a replay: the first row stands.
            .on_conflict_do_nothing(constraint="uq_webhook_deliveries_delivery")
            .returning(WebhookDelivery.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    return made is not None


async def waiting(
    session: AsyncSession, *, before: dt.datetime, limit: int
) -> list[WebhookDelivery]:
    """Deliveries received before `before` that nobody has handled, oldest first.

    The rows are locked until the caller's transaction ends, and rows another watcher
    holds are passed over, so two watchers never work on the same delivery.
    """
    rows = await session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.handled_at.is_(None), WebhookDelivery.received_at <= before)
        .order_by(WebhookDelivery.received_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars())


async def mark_handled(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    check_name: str,
    scope: str,
    upto: dt.datetime,
    outcome: str,
    now: dt.datetime,
) -> None:
    """Every waiting delivery for one setting received up to `upto` is dealt with.

    All of them, not only the ones this pass claimed: one look answers them all, so a
    setting that is sent a thousand deliveries cannot keep others waiting behind it.
    Rows another watcher holds are passed over; it marks those itself. Does not commit.
    """
    waiting_for_setting = (
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.handled_at.is_(None),
            WebhookDelivery.tenant_id == tenant_id,
            WebhookDelivery.check_name == check_name,
            WebhookDelivery.scope == scope,
            WebhookDelivery.received_at <= upto,
        )
        .with_for_update(skip_locked=True)
    )
    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id.in_(waiting_for_setting))
        .values(handled_at=now, outcome=outcome)
    )


async def prune(session: AsyncSession, *, before: dt.datetime) -> None:
    """Delete deliveries dealt with before `before`. Commits.

    A delivery is a wake-up, not a record of anything: what was seen is in the evidence
    log and what it led to is in the drift events. Without this the table would grow by
    one row for every delivery ever sent, including replays.
    """
    await session.execute(delete(WebhookDelivery).where(WebhookDelivery.handled_at < before))
    await session.commit()
