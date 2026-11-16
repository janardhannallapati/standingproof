"""A reviewer's record that a filled file went to a customer.

Step 10 reopens an answer when the setting behind it changes. To do that it must know which
wording each customer was given. This is where that is written down: by a person, after
they sent the file. It records that someone said so, not that anything was delivered.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import FilledFile, RunQuestion, SentFile
from standingproof.writeback.load import file_key, load

MAX_CUSTOMER_CHARS = 200


class NotCurrentFileError(Exception):
    """A decision changed after this file was made. Make and send the current one."""

    def __init__(self) -> None:
        super().__init__("a decision changed after this file was made; download the file again")


@dataclass(frozen=True)
class SentInfo:
    id: uuid.UUID
    customer: str
    sent_by: str
    sent_at: datetime
    answers: int
    # True when the file that was sent is the one for the decisions as they are now.
    current: bool
    # How many of its answers wait again because a live setting they rest on changed.
    reopened: int = 0


async def list_sent(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID, key: str
) -> list[SentInfo]:
    rows = (
        await session.execute(
            select(SentFile, FilledFile.key)
            .join(FilledFile, FilledFile.id == SentFile.filled_file_id)
            .where(SentFile.tenant_id == tenant_id, SentFile.run_id == run_id)
            .order_by(SentFile.sent_at, SentFile.id)
        )
    ).all()
    waiting = {
        str(question_id)
        for question_id in (
            await session.execute(
                select(RunQuestion.question_id).where(
                    RunQuestion.tenant_id == tenant_id,
                    RunQuestion.run_id == run_id,
                    RunQuestion.reopened_at.is_not(None),
                )
            )
        ).scalars()
    }
    return [
        SentInfo(
            s.id,
            s.customer,
            s.sent_by,
            s.sent_at,
            len(s.answers),
            file_key_ == key,
            sum(answer["question_id"] in waiting for answer in s.answers),
        )
        for s, file_key_ in rows
    ]


async def record_sent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    customer: str,
    sent_by: str,
) -> tuple[SentInfo, bool] | None:
    """Record the file as sent to `customer`, or find the record already made.

    Returns the record and whether this call made it, or None if the file is not this
    tenant's file of this run. Raises NotCurrentFileError for a file that was superseded.
    """
    file = (
        await session.execute(
            select(FilledFile.key, FilledFile.written).where(
                FilledFile.id == file_id,
                FilledFile.run_id == run_id,
                FilledFile.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    loaded = await load(session, tenant_id, run_id)
    if file is None or loaded is None:
        return None
    if file.key != file_key(loaded.upload_sha256, loaded.rows):
        raise NotCurrentFileError

    # One entry per question whose wording is in the file (not per cell written).
    written = {entry["position"] for entry in file.written}
    answers = [
        {
            "question_id": str(row.question_id),
            "position": row.position,
            "wording_sha256": hashlib.sha256((row.wording or "").encode()).hexdigest(),
        }
        for row in loaded.rows
        if row.position in written
    ]
    name = customer.strip()
    inserted = (
        await session.execute(
            insert(SentFile)
            .values(
                tenant_id=tenant_id,
                run_id=run_id,
                filled_file_id=file_id,
                customer=name,
                answers=answers,
                sent_by=sent_by,
            )
            # The same file recorded for the same customer again: the first record stands.
            .on_conflict_do_nothing()
            .returning(SentFile.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    stored = (
        (
            await session.execute(
                select(SentFile).where(
                    SentFile.filled_file_id == file_id,
                    SentFile.customer.ilike(name.replace("%", r"\%").replace("_", r"\_")),
                )
            )
        )
        .scalars()
        .first()
    )
    if stored is None:
        return None
    info = SentInfo(
        stored.id, stored.customer, stored.sent_by, stored.sent_at, len(stored.answers), True
    )
    return info, inserted is not None
