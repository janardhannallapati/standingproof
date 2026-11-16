"""What a worker does with a question it holds: keep it, finish it, or let it go.

Every statement here matches only `WHERE lease_owner = me`. A worker that lost its lease
changes nothing, however late it arrives.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import case, false, update
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_FAILED,
    ITEM_NEEDS_REVIEW,
    ITEM_QUEUED,
    ITEM_READY,
    ITEM_RUNNING,
    REVIEW_PENDING,
    REVIEW_REDRAFTING,
    RunQuestion,
)
from standingproof.workflow.leases import NOW, finish_run_if_done, in_seconds
from standingproof.workflow.state import Outcome, Progress

REASON_REDRAFT_FAILED = "redraft_failed"


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
                # A draft, or a reason there is none: either way a person decides next.
                review=REVIEW_PENDING,
                version=RunQuestion.version + 1,
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
    progress: Progress | None = None,
) -> None:
    """Let go of a question that could not be finished now.

    It goes back to the queue after a delay, or is marked failed once it has been tried
    `max_attempts` times. Does nothing if this worker no longer holds it. `refund_attempt`
    is for a worker that is shutting down: stopping is not the question's fault.
    `progress` is what the question has cost so far; it counts against the run's budget
    whether or not the question is ever finished.
    """
    if progress is not None:
        await session.execute(
            update(RunQuestion)
            .where(_held_by(item_id, owner))
            .values(
                steps=progress.steps,
                cost_usd=Decimal(str(round(progress.cost_usd, 6))),
                usage=progress.usage.model_dump(),
            )
        )
    spent = RunQuestion.attempts >= max_attempts
    redrafting = RunQuestion.review == REVIEW_REDRAFTING
    failed = (
        await session.execute(
            update(RunQuestion)
            .where(_held_by(item_id, owner), spent if not refund_attempt else false())
            .values(
                # A redraft that could not be made leaves the earlier draft standing, back
                # in front of the reviewer. A first draft that could not be made has failed.
                status=case((redrafting, ITEM_NEEDS_REVIEW), else_=ITEM_FAILED),
                reason=case((redrafting, REASON_REDRAFT_FAILED), else_=reason),
                review=case((redrafting, REVIEW_PENDING), else_=None),
                version=RunQuestion.version + 1,
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
