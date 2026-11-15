"""What a worker looks for between jobs: work that has fallen through, and runs to close."""

import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_OPEN,
    ITEM_QUEUED,
    ITEM_RUNNING,
    RUN_FINISHED,
    RUN_RUNNING,
    Run,
    RunQuestion,
)
from standingproof.workflow.leases import NOW, in_seconds


async def due_for_queue(
    session: AsyncSession, *, limit: int, requeue_seconds: float
) -> list[uuid.UUID]:
    """Open questions that should be on the queue and may not be. The sweep queues these.

    Covers a job that was never written, a job lost with Redis, a question waiting to be
    retried, and a question whose worker died. Each is pushed forward so that it is not
    returned again until `requeue_seconds` have passed.
    """
    open_and_idle = or_(
        RunQuestion.status == ITEM_QUEUED,
        (RunQuestion.status == ITEM_RUNNING) & (RunQuestion.lease_until < NOW),
    )
    due = open_and_idle & (RunQuestion.enqueue_after <= NOW)
    chosen = (
        select(RunQuestion.id)
        .where(due)
        .order_by(RunQuestion.enqueue_after)
        .limit(limit)
        # Two workers sweeping at once each get different rows.
        .with_for_update(skip_locked=True)
    )
    rows = await session.execute(
        update(RunQuestion)
        .where(RunQuestion.id.in_(chosen))
        .values(enqueue_after=in_seconds(requeue_seconds))
        .returning(RunQuestion.id)
    )
    ids = [row[0] for row in rows]
    await session.commit()
    return ids


async def finish_idle_runs(session: AsyncSession) -> int:
    """Close every running run that has no open question left. Returns how many.

    Normally the worker that finishes the last question closes the run. This is the net
    under that: whatever went wrong there, a run never stays "running" with nothing to do.
    """
    still_open = (
        select(RunQuestion.id)
        .where(RunQuestion.run_id == Run.id, RunQuestion.status.in_(ITEM_OPEN))
        .exists()
    )
    rows = await session.execute(
        update(Run)
        .where(Run.status == RUN_RUNNING, ~still_open)
        .values(status=RUN_FINISHED, finished_at=NOW)
        .returning(Run.id)
    )
    closed = len(rows.all())
    await session.commit()
    return closed
