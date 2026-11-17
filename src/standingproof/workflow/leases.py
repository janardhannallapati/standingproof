"""Who is working on which question: taking, keeping and letting go of a lease.

Postgres is the truth about what work exists and who holds it. The queue only wakes a
worker up. So every function here is safe when a job arrives twice, late, or not at all.

Each function commits its own change before returning.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_FAILED,
    ITEM_NOT_STARTED,
    ITEM_OPEN,
    ITEM_QUEUED,
    ITEM_RUNNING,
    REVIEW_REDRAFTING,
    RUN_FINISHED,
    RUN_RUNNING,
    Question,
    ReviewDecision,
    Run,
    RunQuestion,
)
from standingproof.workflow.budget import room_in_budget

REASON_RUN_COST_LIMIT = "run_cost_limit"
REASON_CANCELLED = "cancelled"

NOW = func.now()


def in_seconds(seconds: float) -> Any:
    return NOW + timedelta(seconds=seconds)


@dataclass(frozen=True)
class Lease:
    """A question a worker now holds, with what it needs to work on it."""

    item_id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    question: str
    attempts: int
    # A reviewer's decision the graph has not taken yet (a redraft), or None.
    resume: dict[str, str] | None = None


async def finish_run_if_done(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Close the run if none of its questions is open. The caller commits afterwards.

    Commits first. Two workers can finish a run's last two questions at the same moment,
    and inside its own transaction each would still see the other's question as open, so
    neither would close the run. After a commit, the later of the two sees both.
    """
    await session.commit()
    still_open = (
        select(RunQuestion.id)
        .where(RunQuestion.run_id == run_id, RunQuestion.status.in_(ITEM_OPEN))
        .exists()
    )
    await session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == RUN_RUNNING, ~still_open)
        .values(status=RUN_FINISHED, finished_at=NOW)
    )


async def _close_unstarted(session: AsyncSession, item_id: uuid.UUID, reason: str) -> None:
    await session.execute(
        update(RunQuestion)
        .where(RunQuestion.id == item_id, RunQuestion.status == ITEM_QUEUED)
        .values(status=ITEM_NOT_STARTED, reason=reason, finished_at=NOW)
    )


async def wait(session: AsyncSession, item_id: uuid.UUID, *, seconds: float) -> None:
    """Leave a question as it is and have the sweep offer it again later. Commits."""
    await session.execute(
        update(RunQuestion)
        .where(RunQuestion.id == item_id, RunQuestion.status.in_((ITEM_QUEUED, ITEM_RUNNING)))
        .values(enqueue_after=in_seconds(seconds))
    )
    await session.commit()


async def tenant_of(session: AsyncSession, item_id: uuid.UUID) -> uuid.UUID | None:
    """Whose question a job names. Asked across tenants: a job carries only the id."""
    return (
        await session.execute(select(RunQuestion.tenant_id).where(RunQuestion.id == item_id))
    ).scalar_one_or_none()


async def take_lease(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    owner: str,
    seconds: float,
    max_attempts: int,
    reserve_usd: float,
    wait_seconds: float = 5.0,
) -> Lease | None:
    """Take a question for this worker, or return None if it is not there to be taken.

    A question can be taken when it is queued, or when it is running and its holder's lease
    has run out (the holder died). One UPDATE decides, so of two workers exactly one wins.

    `reserve_usd` is the most one question may cost. It is set aside for every question in
    progress when deciding whether the run's budget has room for this one.
    """
    run_id = (
        await session.execute(select(RunQuestion.run_id).where(RunQuestion.id == item_id))
    ).scalar_one_or_none()
    if run_id is None:
        return None
    room = await room_in_budget(session, run_id=run_id, item_id=item_id, reserve_usd=reserve_usd)
    if room == "cancelled":
        await _close_unstarted(session, item_id, REASON_CANCELLED)
        await session.commit()
        return None
    if room == "spent":
        await _close_unstarted(session, item_id, REASON_RUN_COST_LIMIT)
        await finish_run_if_done(session, run_id)
        await session.commit()
        return None
    if room == "wait":
        # Questions in progress may still use the rest of the budget. Look again shortly.
        await session.execute(
            update(RunQuestion)
            .where(RunQuestion.id == item_id, RunQuestion.status == ITEM_QUEUED)
            .values(enqueue_after=in_seconds(wait_seconds))
        )
        await session.commit()
        return None

    free = or_(
        RunQuestion.status == ITEM_QUEUED,
        (RunQuestion.status == ITEM_RUNNING) & (RunQuestion.lease_until < NOW),
    )
    # A question taken up too often is given up: something about it kills workers.
    exhausted = (
        await session.execute(
            update(RunQuestion)
            .where(RunQuestion.id == item_id, free, RunQuestion.attempts >= max_attempts)
            .values(
                status=ITEM_FAILED,
                reason="attempts_exhausted",
                finished_at=NOW,
                lease_owner=None,
                lease_until=None,
            )
            .returning(RunQuestion.id)
        )
    ).one_or_none()
    if exhausted is not None:
        await finish_run_if_done(session, run_id)
        await session.commit()
        return None

    taken = (
        await session.execute(
            update(RunQuestion)
            .where(RunQuestion.id == item_id, free)
            .values(
                status=ITEM_RUNNING,
                lease_owner=owner,
                lease_until=in_seconds(seconds),
                attempts=RunQuestion.attempts + 1,
                started_at=func.coalesce(RunQuestion.started_at, NOW),
                # If this worker dies, the sweep may queue the question again the moment
                # the lease runs out, whatever wait was set while it was queued.
                enqueue_after=NOW,
            )
            .returning(
                RunQuestion.tenant_id,
                RunQuestion.question_id,
                RunQuestion.attempts,
                RunQuestion.review,
            )
        )
    ).one_or_none()
    if taken is None:
        await session.commit()
        return None
    tenant_id, question_id, attempts, review = taken
    question = (
        await session.execute(
            select(Question.text).where(Question.tenant_id == tenant_id, Question.id == question_id)
        )
    ).scalar_one()
    resume = None
    if review == REVIEW_REDRAFTING:
        instruction = (
            await session.execute(
                select(ReviewDecision.comment)
                .where(
                    ReviewDecision.run_question_id == item_id,
                    ReviewDecision.decision == "redraft",
                )
                .order_by(ReviewDecision.decided_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        resume = {"decision": "redraft", "instruction": instruction or ""}
    await session.commit()
    return Lease(
        item_id=item_id,
        run_id=run_id,
        tenant_id=tenant_id,
        question=question,
        attempts=attempts,
        resume=resume,
    )
