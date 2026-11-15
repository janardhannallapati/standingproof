"""Start a run over a questionnaire and read how it is going.

The request only records the work and wakes a worker. Answering happens in the worker
process, so a request never waits for a model.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel
from redis.exceptions import RedisError

from standingproof.api.deps import RedisDep, SessionDep, SettingsDep, TenantDep
from standingproof.workflow import runs
from standingproof.workflow.queue import JobQueue

log = logging.getLogger(__name__)
router = APIRouter(tags=["runs"])

ItemStatus = Literal["queued", "running", "ready", "needs_review", "failed", "not_started"]


class RunOut(BaseModel):
    id: uuid.UUID
    questionnaire_id: uuid.UUID
    status: Literal["running", "finished", "cancelled"]
    # How many questions are in each state. States with none are left out.
    counts: dict[str, int]
    cost_usd: Decimal
    max_cost_usd: Decimal
    created_at: datetime
    finished_at: datetime | None


class RunQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    question: str
    status: ItemStatus
    # reused: an approved answer matched exactly. drafted: written by the model from sources.
    path: Literal["reused", "drafted"] | None
    reason: str | None
    # A draft for a reviewer. Nothing here has been approved or sent.
    answer: str | None
    citations: list[str] | None
    note: str | None
    steps: int
    cost_usd: Decimal


class RunQuestionPage(BaseModel):
    items: list[RunQuestionOut]
    next_after: int | None


async def _out(session: SessionDep, tenant_id: uuid.UUID, run_id: uuid.UUID) -> RunOut:
    found = await runs.summary(session, tenant_id=tenant_id, run_id=run_id)
    if found is None:
        # The same answer for "does not exist" and "belongs to someone else".
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    run = found.run
    return RunOut(
        id=run.id,
        questionnaire_id=run.questionnaire_id,
        status=run.status,  # type: ignore[arg-type]
        counts=found.counts,
        cost_usd=found.cost_usd,
        max_cost_usd=run.max_cost_usd,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


@router.post(
    "/questionnaires/{questionnaire_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": RunOut, "description": "A run is already active; this is it."},
        404: {"description": "No such questionnaire."},
        409: {"description": "The questionnaire's columns have not been chosen yet."},
    },
)
async def start_run(
    questionnaire_id: uuid.UUID,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
    redis: RedisDep,
    response: Response,
) -> RunOut:
    """Answer every question of a questionnaire in the background."""
    try:
        started = await runs.start_run(
            session,
            tenant_id=tenant_id,
            questionnaire_id=questionnaire_id,
            max_cost_usd=settings.run_cost_limit_usd,
            enqueue_grace_seconds=settings.requeue_seconds,
        )
    except runs.QuestionnaireNotReadyError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if started is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Questionnaire not found.")

    if not started.created:
        response.status_code = status.HTTP_200_OK
    elif started.item_ids:
        # The rows are committed; this only wakes a worker. If Redis is away the request
        # still succeeds, and a worker's sweep queues the questions when their grace ends.
        try:
            queue = JobQueue(redis, stream=settings.queue_stream, consumer="api")
            await queue.add([str(item_id) for item_id in started.item_ids])
        except (RedisError, OSError) as error:
            log.warning("run %s not queued yet: %s", started.run.id, type(error).__name__)
    return await _out(session, tenant_id, started.run.id)


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> RunOut:
    return await _out(session, tenant_id, run_id)


@router.get("/runs/{run_id}/questions")
async def list_run_questions(
    run_id: uuid.UUID,
    session: SessionDep,
    tenant_id: TenantDep,
    status_filter: Annotated[ItemStatus | None, Query(alias="status")] = None,
    after: Annotated[int, Query(ge=0, description="`next_after` from the previous page.")] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> RunQuestionPage:
    await _out(session, tenant_id, run_id)  # 404 before anything else
    rows = await runs.page_of_questions(
        session, tenant_id=tenant_id, run_id=run_id, status=status_filter, after=after, limit=limit
    )
    items = [
        RunQuestionOut(
            id=item.id,
            position=item.position,
            question=question,
            status=item.status,  # type: ignore[arg-type]
            path=item.path,  # type: ignore[arg-type]
            reason=item.reason,
            answer=item.answer,
            citations=item.citations,
            note=item.note,
            steps=item.steps,
            cost_usd=item.cost_usd,
        )
        for item, question in rows
    ]
    next_after = items[-1].position if len(items) == limit else None
    return RunQuestionPage(items=items, next_after=next_after)


@router.post("/runs/{run_id}/cancel", responses={404: {"description": "No such run."}})
async def cancel_run(run_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> RunOut:
    """Stop starting questions. Ones already in progress finish; nothing is undone."""
    if not await runs.cancel_run(session, tenant_id=tenant_id, run_id=run_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return await _out(session, tenant_id, run_id)
