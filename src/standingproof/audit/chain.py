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
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import SecretBytes
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.db import ACTOR, AUDIT_KEY
from standingproof.models import AuditEntry, AuditHead

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
    # How many entries of the chain do not verify, this one included.
    bad: int = 1


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
    key: bytes | None,
    previous: str,
    tenant_id: uuid.UUID | None,
    seq: int,
    at: dt.datetime,
    actor: str,
    action: str,
    subject: str,
    details: dict[str, Any],
) -> str:
    """A digest of the entry's content and of the hash before it, in one fixed spelling.

    With a key it is an HMAC: whoever can only run SQL cannot compute it, so cannot add
    an entry that verifies. With no key it is a plain SHA-256, which shows that entries
    were not altered or removed and says nothing about who wrote them.
    """
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
    ).encode()
    if key is None:
        return hashlib.sha256(body).hexdigest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _of(tenant_id: uuid.UUID | None) -> Any:
    return (
        AuditEntry.tenant_id.is_(None) if tenant_id is None else AuditEntry.tenant_id == tenant_id
    )


def _key(session: AsyncSession) -> bytes | None:
    key = session.info.get(AUDIT_KEY)
    return key.get_secret_value() if isinstance(key, SecretBytes) else None


async def _take_the_chain(session: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """One writer per chain at a time, until the caller's transaction ends.

    Each entry needs the hash of the one before it, so two writers cannot both be next.
    The lock is a row, not a named (advisory) lock: a named lock can be taken by anyone
    who knows the name, and one tenant could then hold another's chain. A row is governed
    by the policies, so a tenant can lock its own chain and no other.
    """
    await session.execute(
        insert(AuditHead)
        .values(tenant_id=tenant_id)
        .on_conflict_do_nothing(constraint="uq_audit_heads_tenant")
    )
    head_of = (
        AuditHead.tenant_id.is_(None) if tenant_id is None else AuditHead.tenant_id == tenant_id
    )
    await session.execute(select(AuditHead.id).where(head_of).with_for_update())


async def record(session: AsyncSession, tenant_id: uuid.UUID | None, *entries: Entry) -> None:
    """Append entries to a chain. Does not commit: the caller's action commits with them."""
    if not entries:
        return
    for entry in entries:
        _check_plain(entry.details)
    await _take_the_chain(session, tenant_id)
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
    key = _key(session)
    rows = []
    for entry in entries:
        seq += 1
        actor = entry.actor or str(session.info.get(ACTOR) or "system")
        digest = entry_hash(
            key=key,
            previous=previous,
            tenant_id=tenant_id,
            seq=seq,
            at=at,
            actor=actor,
            action=entry.action,
            subject=entry.subject,
            details=entry.details,
        )
        rows.append(
            {
                "tenant_id": tenant_id,
                "seq": seq,
                "at": at,
                "actor": actor,
                "action": entry.action,
                "subject": entry.subject,
                "details": entry.details,
                "prev_hash": previous,
                "hash": digest,
            }
        )
        previous = digest
    # One at a time and in order: the database checks each against the one before it.
    for row in rows:
        await session.execute(insert(AuditEntry).values(**row))


_UNSET: Any = object()


async def verify(
    session: AsyncSession, tenant_id: uuid.UUID | None, *, key: bytes | None = _UNSET
) -> tuple[int, Broken | None]:
    """Recompute a chain from its first entry. Returns how many entries hold, and (when
    not all do) the first that does not with how many do not.

    It goes on past a bad entry, taking that entry's stored hash as the link to the next:
    one bad entry is then reported as one, and the entries after it are still checked.
    """
    if key is _UNSET:
        key = _key(session)
    rows = await session.stream_scalars(
        select(AuditEntry).where(_of(tenant_id)).order_by(AuditEntry.seq)
    )
    sound, bad, first, expected, previous = 0, 0, None, 1, GENESIS
    async for row in rows:
        reason = None
        if row.seq != expected:
            reason = f"entry {expected} is missing (found {row.seq})"
        elif row.prev_hash != previous:
            reason = "does not follow the entry before it"
        else:
            digest = entry_hash(
                key=key,
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
                reason = "its content does not match its hash"
        if reason is None:
            sound += 1
        else:
            bad += 1
            first = first or (expected if row.seq != expected else row.seq, reason)
        expected, previous = row.seq + 1, row.hash
    if first is None:
        return sound, None
    return sound, Broken(first[0], first[1], bad)


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
