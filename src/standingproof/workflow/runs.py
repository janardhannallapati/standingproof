"""Runs over a questionnaire: starting one, stopping one, and reading how it is going.

Each function that changes data commits before returning.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_NOT_STARTED,
    ITEM_OPEN,
    ITEM_QUEUED,
    REVIEW_EXPIRED,
    REVIEW_PENDING,
    REVIEW_REDRAFTING,
    RUN_CANCELLED,
    RUN_FINISHED,
    RUN_RUNNING,
    STATUS_PARSED,
    Question,
    Questionnaire,
    Run,
    RunQuestion,
)
from standingproof.workflow.leases import NOW, REASON_CANCELLED, in_seconds


class QuestionnaireNotReadyError(Exception):
    """A run was asked for on a questionnaire whose questions have not been read yet."""


@dataclass(frozen=True)
class StartedRun:
    run: Run
    created: bool
    # The questions to put on the queue. Empty when the run already existed.
    item_ids: list[uuid.UUID]


async def _active_run(session: AsyncSession, questionnaire_id: uuid.UUID) -> Run | None:
    statement = select(Run).where(
        Run.questionnaire_id == questionnaire_id, Run.status == RUN_RUNNING
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def start_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    questionnaire_id: uuid.UUID,
    max_cost_usd: float,
    enqueue_grace_seconds: float,
) -> StartedRun | None:
    """Create a run with one queued row per question. None if the questionnaire is not ours.

    Starting a run while one is active returns the active one: two would pay twice.
    """
    questionnaire = (
        await session.execute(
            select(Questionnaire).where(
                Questionnaire.tenant_id == tenant_id, Questionnaire.id == questionnaire_id
            )
        )
    ).scalar_one_or_none()
    if questionnaire is None:
        return None
    if questionnaire.status != STATUS_PARSED:
        raise QuestionnaireNotReadyError("the questionnaire's columns have not been chosen yet")

    active = await _active_run(session, questionnaire_id)
    if active is not None:
        return StartedRun(run=active, created=False, item_ids=[])

    run = Run(
        tenant_id=tenant_id,
        questionnaire_id=questionnaire_id,
        status=RUN_RUNNING,
        max_cost_usd=Decimal(str(max_cost_usd)),
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError:
        # Another request started a run between our look and our insert.
        await session.rollback()
        active = await _active_run(session, questionnaire_id)
        if active is None:
            raise
        return StartedRun(run=active, created=False, item_ids=[])

    questions = (
        await session.execute(
            select(Question.id, Question.position)
            .where(Question.tenant_id == tenant_id, Question.questionnaire_id == questionnaire_id)
            .order_by(Question.position)
        )
    ).all()
    item_ids: list[uuid.UUID] = []
    if questions:
        rows = await session.execute(
            insert(RunQuestion)
            .values(
                [
                    {
                        "tenant_id": tenant_id,
                        "run_id": run.id,
                        "question_id": question_id,
                        "position": position,
                        "status": ITEM_QUEUED,
                        # The caller queues these now. If that fails, the sweep does it then.
                        "enqueue_after": in_seconds(enqueue_grace_seconds),
                    }
                    for question_id, position in questions
                ]
            )
            .returning(RunQuestion.id)
        )
        item_ids = [row[0] for row in rows]
    else:
        run.status = RUN_FINISHED
        run.finished_at = NOW
    await session.commit()
    await session.refresh(run)
    return StartedRun(run=run, created=True, item_ids=item_ids)


async def cancel_run(session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """Stop a run: queued questions are closed; ones in progress finish. False if not ours."""
    run = (
        await session.execute(select(Run).where(Run.tenant_id == tenant_id, Run.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return False
    if run.status == RUN_RUNNING:
        run.status = RUN_CANCELLED
        run.finished_at = NOW
        await session.execute(
            update(RunQuestion)
            .where(RunQuestion.run_id == run_id, RunQuestion.status == ITEM_QUEUED)
            .values(status=ITEM_NOT_STARTED, reason=REASON_CANCELLED, finished_at=NOW)
        )
    await session.commit()
    return True


@dataclass(frozen=True)
class RunSummary:
    run: Run
    counts: dict[str, int]
    cost_usd: Decimal


async def summary(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> RunSummary | None:
    run = (
        await session.execute(select(Run).where(Run.tenant_id == tenant_id, Run.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return None
    rows = (
        await session.execute(
            select(
                RunQuestion.status, func.count(), func.coalesce(func.sum(RunQuestion.cost_usd), 0)
            )
            .where(RunQuestion.tenant_id == tenant_id, RunQuestion.run_id == run_id)
            .group_by(RunQuestion.status)
        )
    ).all()
    return RunSummary(
        run=run,
        counts={status: count for status, count, _ in rows},
        cost_usd=sum((cost for _, _, cost in rows), Decimal(0)),
    )


async def page_of_questions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    status: str | None,
    after: int,
    limit: int,
) -> list[tuple[RunQuestion, str]]:
    """Questions of a run in sheet order, each with its text, after position `after`."""
    statement = (
        select(RunQuestion, Question.text)
        .join(Question, Question.id == RunQuestion.question_id)
        .where(
            RunQuestion.tenant_id == tenant_id,
            RunQuestion.run_id == run_id,
            RunQuestion.position > after,
        )
        .order_by(RunQuestion.position)
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(RunQuestion.status == status)
    return [(item, question) for item, question in await session.execute(statement)]


async def expire_unreviewed(session: AsyncSession, *, keep_days: int) -> int:
    """Close questions that have waited for a decision longer than `keep_days`.

    Nothing is approved or rejected: they become `expired`, which no file is ever written
    from, and their saved state can then be deleted like any other.
    """
    rows = await session.execute(
        update(RunQuestion)
        .where(
            RunQuestion.review == REVIEW_PENDING,
            RunQuestion.finished_at < NOW - timedelta(days=keep_days),
        )
        .values(review=REVIEW_EXPIRED, version=RunQuestion.version + 1, reviewed_at=NOW)
        .returning(RunQuestion.id)
    )
    expired = len(rows.all())
    await session.commit()
    return expired


async def finished_with_state(
    session: AsyncSession, *, keep_days: int, limit: int
) -> list[uuid.UUID]:
    """Questions whose saved state is no longer needed and is still there.

    A question waiting for a decision, or being redrafted, keeps its state however old it
    is: a redraft continues from it. Otherwise the clock starts at the decision, or at the
    end of the work for a question nobody can review (failed, never started).
    """
    settled = func.coalesce(RunQuestion.reviewed_at, RunQuestion.finished_at)
    rows = await session.execute(
        select(RunQuestion.id)
        .where(
            RunQuestion.state_deleted_at.is_(None),
            RunQuestion.status.not_in(ITEM_OPEN),
            RunQuestion.review.is_distinct_from(REVIEW_PENDING),
            RunQuestion.review.is_distinct_from(REVIEW_REDRAFTING),
            settled < NOW - timedelta(days=keep_days),
        )
        .limit(limit)
    )
    return [row[0] for row in rows]


async def mark_state_deleted(session: AsyncSession, item_ids: list[uuid.UUID]) -> None:
    if item_ids:
        await session.execute(
            update(RunQuestion).where(RunQuestion.id.in_(item_ids)).values(state_deleted_at=NOW)
        )
    await session.commit()
