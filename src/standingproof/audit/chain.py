"""The hash chain: appending an entry, and checking the chain from its first entry.

One chain per tenant, and one for the deployment (tenant None). An entry is written in the
same transaction as the action it records, so neither exists without the other.

What a sound chain proves: nobody who could change single rows did so. What it does not:
that an owner of the database did not rebuild the whole chain, or cut it off at the end.
The defence against both is to keep the head hash (`head`) somewhere the database's owner
cannot reach, and compare.
"""

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.db import ACTOR
from standingproof.models import AuditEntry

GENESIS = "0" * 64
# Values that Postgres JSONB gives back exactly as they went in. A float does not qualify
# (1.0 comes back as 1.0 or 1, 1e2 as 100), and a hash must be recomputed from what is read.
_PLAIN = (str, int, bool, type(None))


@dataclass(frozen=True)
class Entry:
    """One thing to record: who did what to which thing, and the details worth keeping."""

    action: str
    subject: str
    details: dict[str, Any]
    # Who did it. None: whoever the session acts for (see `standingproof.db`), which is a
    # person for a request and "system" for a worker or the watcher.
    actor: str | None = None


@dataclass(frozen=True)
class Broken:
    """Where a chain first fails, counted from its first entry."""

    seq: int
    reason: str


def _check_plain(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("audit details: a key must be a string")
            _check_plain(item)
    elif isinstance(value, list):
        for item in value:
            _check_plain(item)
    elif not isinstance(value, _PLAIN):
        raise TypeError(f"audit details: {type(value).__name__} does not survive storage unchanged")


def entry_hash(
    *,
    previous: str,
    tenant_id: uuid.UUID | None,
    seq: int,
    at: dt.datetime,
    actor: str,
    action: str,
    subject: str,
    details: dict[str, Any],
) -> str:
    """SHA-256 over the entry's content and the hash before it, in one fixed spelling."""
    body = json.dumps(
        [
            previous,
            None if tenant_id is None else str(tenant_id),
            seq,
            at.astimezone(dt.UTC).isoformat(timespec="microseconds"),
            actor,
            action,
            subject,
            details,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _of(tenant_id: uuid.UUID | None) -> Any:
    return (
        AuditEntry.tenant_id.is_(None) if tenant_id is None else AuditEntry.tenant_id == tenant_id
    )


async def record(session: AsyncSession, tenant_id: uuid.UUID | None, *entries: Entry) -> None:
    """Append entries to a chain. Does not commit: the caller's action commits with them.

    One writer per chain at a time, until the caller's transaction ends: each entry needs
    the hash of the one before it, so two writers cannot both be next.
    """
    for entry in entries:
        _check_plain(entry.details)
    key = f"audit:{tenant_id}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
    last = (
        await session.execute(
            select(AuditEntry.seq, AuditEntry.hash)
            .where(_of(tenant_id))
            .order_by(AuditEntry.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    seq, previous = (0, GENESIS) if last is None else (last.seq, last.hash)
    # The database's clock, read once: the time is part of what is hashed, so it must be
    # the value that is stored, to the microsecond.
    at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    for entry in entries:
        seq += 1
        actor = entry.actor or str(session.info.get(ACTOR) or "system")
        digest = entry_hash(
            previous=previous,
            tenant_id=tenant_id,
            seq=seq,
            at=at,
            actor=actor,
            action=entry.action,
            subject=entry.subject,
            details=entry.details,
        )
        session.add(
            AuditEntry(
                tenant_id=tenant_id,
                seq=seq,
                at=at,
                actor=actor,
                action=entry.action,
                subject=entry.subject,
                details=entry.details,
                prev_hash=previous,
                hash=digest,
            )
        )
        previous = digest
    await session.flush()


async def verify(session: AsyncSession, tenant_id: uuid.UUID | None) -> tuple[int, Broken | None]:
    """Recompute a chain from its first entry. Returns how many entries were sound, and
    where it first fails (None when all of it holds)."""
    rows = await session.stream_scalars(
        select(AuditEntry).where(_of(tenant_id)).order_by(AuditEntry.seq)
    )
    sound, previous = 0, GENESIS
    async for row in rows:
        expected = sound + 1
        if row.seq != expected:
            return sound, Broken(expected, f"entry {expected} is missing (found {row.seq})")
        if row.prev_hash != previous:
            return sound, Broken(row.seq, "does not follow the entry before it")
        digest = entry_hash(
            previous=previous,
            tenant_id=row.tenant_id,
            seq=row.seq,
            at=row.at,
            actor=row.actor,
            action=row.action,
            subject=row.subject,
            details=row.details,
        )
        if digest != row.hash:
            return sound, Broken(row.seq, "its content does not match its hash")
        sound, previous = row.seq, row.hash
    return sound, None


async def head(session: AsyncSession, tenant_id: uuid.UUID | None) -> tuple[int, str]:
    """The number and hash of a chain's last entry: what to keep elsewhere and compare."""
    last = (
        await session.execute(
            select(AuditEntry.seq, AuditEntry.hash)
            .where(_of(tenant_id))
            .order_by(AuditEntry.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    return (0, GENESIS) if last is None else (last.seq, last.hash)
