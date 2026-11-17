"""Which observation each answer rests on, and which settings are due another look."""

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_NEEDS_REVIEW,
    ITEM_READY,
    REVIEW_APPROVED,
    REVIEW_EDITED,
    REVIEW_PENDING,
    AnswerEvidence,
    DriftQueue,
    Evidence,
    Limit,
    RunQuestion,
)

_PREFIX = "evidence:"
# An answer a customer has, may get, or a reviewer is about to decide. A rejected,
# expired or unanswered question says nothing that a changed setting could contradict.
# The reason on a look that the stop switch refused (see checks/service.py).
STOPPED = "stopped"
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
    # Through a function: the app role holds DELETE on no table (migration 0017).
    await session.execute(select(func.forget_answer_evidence(run_question_id)))
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
                order_by=Evidence.seq.desc(),
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
    await queue_newer(session, run_question_id)


async def queue_newer(session: AsyncSession, run_question_id: uuid.UUID) -> None:
    """Have this answer compared with any reading newer than the one it rests on.

    A change is examined once, against the answers that are waiting or decided at that
    moment. An answer that was still being drafted, or redrafted, was passed over. When
    it comes to rest (a draft is saved, a failed redraft puts the earlier draft back) the
    newest reading of each of its settings is queued again, in the caller's transaction,
    so the examiner sees this answer too. Does not commit.
    """
    links = await session.execute(
        select(AnswerEvidence).where(AnswerEvidence.run_question_id == run_question_id)
    )
    for link in links.scalars():
        newest = (
            await session.execute(
                select(Evidence.id)
                .where(
                    Evidence.tenant_id == link.tenant_id,
                    Evidence.check_name == link.check_name,
                    Evidence.scope == link.scope,
                    Evidence.status == "observed",
                )
                .order_by(Evidence.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if newest is not None and newest != link.evidence_id:
            await session.execute(
                insert(DriftQueue)
                .values(evidence_id=newest, tenant_id=link.tenant_id, trigger="run")
                .on_conflict_do_nothing(index_elements=[DriftQueue.evidence_id])
            )


def not_stopped(tenant_id: Any) -> Any:
    """True for a tenant that neither its own switch nor the deployment's has stopped."""
    return ~(
        select(Limit.id)
        .where(Limit.stopped, (Limit.tenant_id == tenant_id) | Limit.tenant_id.is_(None))
        .exists()
    )


async def is_watched(session: AsyncSession, setting: "Setting") -> bool:
    """True when some answer rests on this setting."""
    found = await session.execute(
        select(AnswerEvidence.run_question_id)
        .where(
            AnswerEvidence.tenant_id == setting.tenant_id,
            AnswerEvidence.check_name == setting.check_name,
            AnswerEvidence.scope == setting.scope,
        )
        .limit(1)
    )
    return found.first() is not None


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
        .where(
            RunQuestion.status.in_(LIVE_STATUSES),
            RunQuestion.review.in_(LIVE_REVIEWS),
            # Left out here, before the batch is cut: a stopped tenant's settings stay due
            # for as long as it is stopped, and would otherwise fill every batch.
            not_stopped(AnswerEvidence.tenant_id),
        )
        .distinct()
        .subquery()
    )
    last = (
        select(Evidence.status, Evidence.observed_at)
        .where(
            Evidence.tenant_id == watched.c.tenant_id,
            Evidence.check_name == watched.c.check_name,
            Evidence.scope == watched.c.scope,
            # Refused by the stop switch: nothing was looked at, so it does not count.
            Evidence.reason.is_distinct_from(STOPPED),
        )
        .order_by(Evidence.seq.desc())
        .limit(1)
        .lateral()
    )
    rows = await session.execute(
        select(watched, last.c.status, last.c.observed_at)
        .join(last, true())
        .order_by(last.c.observed_at)
    )
    found = []
    for tenant_id, check_name, scope, status, looked_at in rows:
        wait = retry if status == "unavailable" else recheck
        if looked_at + wait <= now:
            found.append(Setting(tenant_id, check_name, scope))
    return found[:limit]
