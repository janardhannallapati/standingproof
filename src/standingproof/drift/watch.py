"""Which observation each answer rests on, and which settings are due another look."""

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_NEEDS_REVIEW,
    ITEM_READY,
    REVIEW_APPROVED,
    REVIEW_EDITED,
    REVIEW_PENDING,
    AnswerEvidence,
    Evidence,
    RunQuestion,
)

_PREFIX = "evidence:"
# An answer a customer has, may get, or a reviewer is about to decide. A rejected,
# expired or unanswered question says nothing that a changed setting could contradict.
LIVE_STATUSES = (ITEM_READY, ITEM_NEEDS_REVIEW)
LIVE_REVIEWS = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_EDITED)


@dataclass(frozen=True)
class Setting:
    tenant_id: uuid.UUID
    check_name: str
    scope: str


def _evidence_ids(citations: list[str]) -> list[uuid.UUID]:
    found = []
    for source in citations:
        if not source.startswith(_PREFIX):
            continue
        try:
            found.append(uuid.UUID(source[len(_PREFIX) :]))
        except ValueError:
            continue
    return found


async def rest_on(
    session: AsyncSession, tenant_id: uuid.UUID, run_question_id: uuid.UUID, citations: list[str]
) -> None:
    """Record the settings this answer rests on: one row per live check it cites.

    Replaces what was recorded before, so a redraft rests on what the new draft cites.
    Does not commit: the caller saves the draft in the same transaction.
    """
    await session.execute(
        delete(AnswerEvidence).where(AnswerEvidence.run_question_id == run_question_id)
    )
    ids = _evidence_ids(citations)
    if not ids:
        return
    cited = (
        select(
            Evidence.check_name,
            Evidence.scope,
            Evidence.id,
            # Two observations of one setting cited together: the answer rests on the newer.
            func.row_number()
            .over(
                partition_by=(Evidence.check_name, Evidence.scope),
                order_by=(Evidence.observed_at.desc(), Evidence.id),
            )
            .label("newest"),
        )
        .where(
            Evidence.tenant_id == tenant_id,
            Evidence.id.in_(ids),
            # Only something that was seen can be rested on.
            Evidence.status == "observed",
        )
        .subquery()
    )
    for check_name, scope, evidence_id in await session.execute(
        select(cited.c.check_name, cited.c.scope, cited.c.id).where(cited.c.newest == 1)
    ):
        await session.execute(
            insert(AnswerEvidence).values(
                run_question_id=run_question_id,
                check_name=check_name,
                scope=scope,
                tenant_id=tenant_id,
                evidence_id=evidence_id,
            )
        )


async def due(
    session: AsyncSession,
    *,
    now: dt.datetime,
    recheck: dt.timedelta,
    retry: dt.timedelta,
    limit: int,
) -> list[Setting]:
    """Settings a live answer rests on that have not been looked at lately.

    There is no schedule to keep: whether a setting is due follows from its last row in
    the evidence log. After an observation it is due `recheck` later; after a look that
    could not reach the system, `retry` later. Oldest first.
    """
    watched = (
        select(AnswerEvidence.tenant_id, AnswerEvidence.check_name, AnswerEvidence.scope)
        .join(RunQuestion, RunQuestion.id == AnswerEvidence.run_question_id)
        .where(RunQuestion.status.in_(LIVE_STATUSES), RunQuestion.review.in_(LIVE_REVIEWS))
        .distinct()
        .subquery()
    )
    last = (
        select(Evidence.status, Evidence.observed_at)
        .where(
            Evidence.tenant_id == watched.c.tenant_id,
            Evidence.check_name == watched.c.check_name,
            Evidence.scope == watched.c.scope,
        )
        .order_by(Evidence.observed_at.desc())
        .limit(1)
        .lateral()
    )
    rows = await session.execute(
        select(watched, last.c.status, last.c.observed_at)
        .join(last, func.true())
        .order_by(last.c.observed_at)
    )
    found = []
    for tenant_id, check_name, scope, status, looked_at in rows:
        wait = retry if status == "unavailable" else recheck
        if looked_at + wait <= now:
            found.append(Setting(tenant_id, check_name, scope))
    return found[:limit]
