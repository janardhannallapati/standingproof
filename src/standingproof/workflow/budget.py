"""Whether a run's cost ceiling has room for one more question.

Counted against the ceiling: what every question of the run has cost so far (finished,
failed or given back), plus the most each question now in progress could still cost.
Without the second part, several workers could each see "under the ceiling" at the same
moment and together go far over it.
"""

import uuid
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import ITEM_RUNNING, RUN_RUNNING, Run, RunQuestion

Room = Literal["ok", "wait", "spent", "cancelled"]


async def room_in_budget(
    session: AsyncSession, *, run_id: uuid.UUID, item_id: uuid.UUID, reserve_usd: float
) -> Room:
    """Decide for one question, holding a lock on its run until the caller commits.

    The lock makes the decision and the lease that follows it one step: two workers
    deciding for two questions of the same run do so one after the other.

    - ok: there is room.
    - wait: there would be room if the questions in progress cost nothing more. Try later.
    - spent: the ceiling is reached by money already spent. This question will not start.
    - cancelled: the run is no longer running.

    The run can end at most one question's ceiling over its own: the last question to
    start is allowed whenever any room is left.
    """
    status, ceiling = (
        await session.execute(
            select(Run.status, Run.max_cost_usd).where(Run.id == run_id).with_for_update()
        )
    ).one()
    if status != RUN_RUNNING:
        return "cancelled"
    spent, in_progress = (
        await session.execute(
            select(
                func.coalesce(func.sum(RunQuestion.cost_usd), 0),
                func.count().filter(
                    RunQuestion.status == ITEM_RUNNING,
                    RunQuestion.lease_until >= func.now(),
                    RunQuestion.id != item_id,
                ),
            ).where(RunQuestion.run_id == run_id)
        )
    ).one()
    if spent >= ceiling:
        return "spent"
    if spent + in_progress * Decimal(str(reserve_usd)) >= ceiling:
        return "wait"
    return "ok"
