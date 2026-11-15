"""Who is working on which question: taking, keeping and letting go of a lease.

Postgres is the truth about what work exists and who holds it. The queue only wakes a
worker up. So every function here is safe when a job arrives twice, late, or not at all.

Each function commits its own change before returning.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_FAILED,
    ITEM_NEEDS_REVIEW,
    ITEM_NOT_STARTED,
    ITEM_OPEN,
    ITEM_QUEUED,
    ITEM_READY,
    ITEM_RUNNING,
    RUN_FINISHED,
    RUN_RUNNING,
    Question,
    Run,
    RunQuestion,
)
from standingproof.workflow.state import Outcome

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


async def take_lease(
    session: AsyncSession, *, item_id: uuid.UUID, owner: str, seconds: float, max_attempts: int
) -> Lease | None:
    """Take a question for this worker, or return None if it is not there to be taken.

    A question can be taken when it is queued, or when it is running and its holder's lease
    has run out (the holder died). One UPDATE decides, so of two workers exactly one wins.
    """
    found = (
        await session.execute(
            select(RunQuestion.run_id, Run.status, Run.max_cost_usd)
            .join(Run, Run.id == RunQuestion.run_id)
            .where(RunQuestion.id == item_id)
        )
    ).one_or_none()
    if found is None:
        return None
    run_id, run_status, max_cost = found

    if run_status != RUN_RUNNING:
        await _close_unstarted(session, item_id, REASON_CANCELLED)
        await session.commit()
        return None
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(RunQuestion.cost_usd), 0)).where(
                RunQuestion.run_id == run_id
            )
        )
    ).scalar_one()
    if spent >= max_cost:
        await _close_unstarted(session, item_id, REASON_RUN_COST_LIMIT)
        await finish_run_if_done(session, run_id)
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
            )
            .returning(RunQuestion.tenant_id, RunQuestion.question_id, RunQuestion.attempts)
        )
    ).one_or_none()
    if taken is None:
        await session.commit()
        return None
    tenant_id, question_id, attempts = taken
    question = (
        await session.execute(
            select(Question.text).where(Question.tenant_id == tenant_id, Question.id == question_id)
        )
    ).scalar_one()
    await session.commit()
    return Lease(
        item_id=item_id, run_id=run_id, tenant_id=tenant_id, question=question, attempts=attempts
    )


def _held_by(item_id: uuid.UUID, owner: str) -> Any:
    return (
        (RunQuestion.id == item_id)
        & (RunQuestion.status == ITEM_RUNNING)
        & (RunQuestion.lease_owner == owner)
    )


async def renew_lease(
    session: AsyncSession, *, item_id: uuid.UUID, owner: str, seconds: float
) -> bool:
    """Extend the lease. False means it was lost and the work should stop."""
    row = await session.execute(
        update(RunQuestion)
        .where(_held_by(item_id, owner))
        .values(lease_until=in_seconds(seconds))
        .returning(RunQuestion.id)
    )
    renewed = row.one_or_none() is not None
    await session.commit()
    return renewed


async def save_result(
    session: AsyncSession, *, item_id: uuid.UUID, owner: str, outcome: Outcome
) -> bool:
    """Record how the question ended. False: this worker no longer holds it; nothing saved."""
    draft = outcome.draft
    row = (
        await session.execute(
            update(RunQuestion)
            .where(_held_by(item_id, owner))
            .values(
                status=ITEM_READY if outcome.status == "ready" else ITEM_NEEDS_REVIEW,
                path=outcome.path,
                reason=outcome.reason,
                answer=draft.answer if draft else None,
                citations=draft.citations if draft else None,
                note=draft.note if draft else None,
                steps=outcome.steps,
                cost_usd=Decimal(str(round(outcome.cost_usd, 6))),
                usage=outcome.usage.model_dump(),
                finished_at=NOW,
                lease_owner=None,
                lease_until=None,
            )
            .returning(RunQuestion.run_id)
        )
    ).one_or_none()
    if row is not None:
        await finish_run_if_done(session, row[0])
    await session.commit()
    return row is not None


async def give_back(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    owner: str,
    reason: str,
    retry_in_seconds: float,
    max_attempts: int,
    refund_attempt: bool = False,
) -> None:
    """Let go of a question that could not be finished now.

    It goes back to the queue after a delay, or is marked failed once it has been tried
    `max_attempts` times. Does nothing if this worker no longer holds it. `refund_attempt`
    is for a worker that is shutting down: stopping is not the question's fault.
    """
    spent = RunQuestion.attempts >= max_attempts
    failed = (
        await session.execute(
            update(RunQuestion)
            .where(_held_by(item_id, owner), spent if not refund_attempt else false())
            .values(
                status=ITEM_FAILED,
                reason=reason,
                finished_at=NOW,
                lease_owner=None,
                lease_until=None,
            )
            .returning(RunQuestion.run_id)
        )
    ).one_or_none()
    if failed is not None:
        await finish_run_if_done(session, failed[0])
    else:
        await session.execute(
            update(RunQuestion)
            .where(_held_by(item_id, owner))
            .values(
                status=ITEM_QUEUED,
                reason=reason,
                attempts=RunQuestion.attempts - (1 if refund_attempt else 0),
                lease_owner=None,
                lease_until=None,
                enqueue_after=in_seconds(retry_in_seconds),
            )
        )
    await session.commit()
