"""The stop switch and the ceilings, as rows in Postgres.

Postgres and not Redis: a switch that is lost when a cache is flushed would switch the
spending back on without anyone deciding to. Every change is written to the deployment's
audit chain in the same transaction.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.audit import Entry, record
from standingproof.config import Settings
from standingproof.models import Limit


@dataclass(frozen=True)
class State:
    """What applies to one tenant right now."""

    stopped: bool
    tenant_daily_usd: Decimal
    deployment_daily_usd: Decimal


async def read(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> State:
    """The tenant's own row and the deployment's, with the settings filling what neither
    states. Stopped if either says so. Read fresh on every call: a stop takes effect at
    the next model call or look, not after a cache runs out."""
    rows = (
        await session.execute(
            select(Limit.tenant_id, Limit.stopped, Limit.daily_limit_usd).where(
                (Limit.tenant_id == tenant_id) | Limit.tenant_id.is_(None)
            )
        )
    ).all()
    own = next((row for row in rows if row.tenant_id == tenant_id), None)
    shared = next((row for row in rows if row.tenant_id is None), None)

    def ceiling(row: object, default: float) -> Decimal:
        chosen = getattr(row, "daily_limit_usd", None)
        return chosen if chosen is not None else Decimal(str(default))

    return State(
        stopped=any(row.stopped for row in rows),
        tenant_daily_usd=ceiling(own, settings.tenant_daily_limit_usd),
        deployment_daily_usd=ceiling(shared, settings.daily_limit_usd),
    )


def _subject(tenant_id: uuid.UUID | None) -> str:
    return "deployment" if tenant_id is None else f"tenant:{tenant_id}"


async def _change(
    session: AsyncSession, tenant_id: uuid.UUID | None, by: str, action: str, **values: object
) -> None:
    await session.execute(
        insert(Limit)
        .values(tenant_id=tenant_id, changed_by=by, **values)
        .on_conflict_do_update(
            constraint="uq_limits_tenant",
            set_={**values, "changed_by": by, "changed_at": func.now()},
        )
    )
    details = {key: None if value is None else str(value) for key, value in values.items()}
    # An operator's act, so it goes in the deployment's chain whichever tenant it is about.
    await record(
        session, None, Entry(action=action, subject=_subject(tenant_id), details=details, actor=by)
    )
    await session.commit()


async def set_stopped(
    session: AsyncSession, tenant_id: uuid.UUID | None, stopped: bool, *, by: str
) -> None:
    """Stop or resume one tenant, or (tenant None) the whole deployment. Commits."""
    await _change(
        session, tenant_id, by, "limits.stop" if stopped else "limits.resume", stopped=stopped
    )


async def set_daily_limit(
    session: AsyncSession, tenant_id: uuid.UUID | None, usd: Decimal | None, *, by: str
) -> None:
    """Set a daily ceiling, or (None) go back to the one in the settings. Commits."""
    if usd is not None and usd < 0:
        raise ValueError("a ceiling cannot be negative")
    await _change(session, tenant_id, by, "limits.daily", daily_limit_usd=usd)
