"""What a reviewer needs: one question with the text of its sources, and a way to decide."""

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy import func, select

from standingproof.api.deps import RedisDep, SessionDep, SettingsDep, TenantDep
from standingproof.api.drift import ReopeningOut, reopening_out
from standingproof.api.runs import ItemStatus
from standingproof.drift.view import reopening_of
from standingproof.library import sources
from standingproof.models import (
    REVIEW_REDRAFTING,
    DraftCheck,
    Question,
    Questionnaire,
    Run,
    RunQuestion,
)
from standingproof.workflow import review
from standingproof.workflow.events import publish_changed
from standingproof.workflow.queue import JobQueue

log = logging.getLogger(__name__)
router = APIRouter(tags=["review"])

_UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT
ReviewState = Literal["pending", "approved", "edited", "rejected", "redrafting", "expired"]


class SourceOut(BaseModel):
    id: str
    # False when the source a draft cited can no longer be found (deleted since).
    available: bool
    kind: str | None = None
    text: str | None = None
    label: str | None = None
    owner: str | None = None
    valid_until: date | None = None
    observed_at: datetime | None = None


class FindingOut(BaseModel):
    kind: Literal["unsupported", "contradicted", "not_answered"]
    # Words of the draft. Written by a model from text an outsider controls: show as text.
    quote: str
    # True when `quote` is in the checked wording character for character.
    located: bool
    source_id: str | None
    explanation: str


class DraftCheckOut(BaseModel):
    """What the independent checker made of the agent's draft."""

    outcome: Literal["agrees", "disagrees", "not_checked"]
    findings: list[FindingOut]
    model: str | None
    # False when the wording now shown is not the wording that was checked (it was
    # edited by a reviewer since). The check says nothing about the new wording.
    on_current_answer: bool


class ReviewQuestionOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    position: int
    question: str
    status: ItemStatus
    path: Literal["reused", "drafted"] | None
    reason: str | None
    # What the agent drafted. A proposal, never an approved answer.
    answer: str | None
    note: str | None
    sources: list[SourceOut]
    review: ReviewState | None
    # What a reviewer approved. Only this may ever be written into a customer's file.
    final_answer: str | None
    # Send this back with a decision. A decision on an older version is refused.
    version: int
    redrafts: int
    steps: int
    cost_usd: Decimal
    # Strong terms in an edited answer that no cited source uses.
    warnings: list[str] = []
    # None: the draft was not sent to the checker (reused, nothing drafted, or switched off).
    check: DraftCheckOut | None = None
    # Set while the question waits again because a live setting it rests on has changed:
    # what changed, and which customers have the approved wording.
    reopening: ReopeningOut | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `keep` is for a reopened question only: the answer stands as it was.
    decision: Literal["approve", "edit", "reject", "redraft", "keep"]
    version: int = Field(ge=0)
    # For `edit`: the wording to approve.
    answer: str | None = None
    # For `reject`: why. For `redraft`: what the new draft should do differently.
    comment: str | None = None


class ApproveReadyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How many ready questions the reviewer was shown. Refused if that has changed.
    count: int = Field(ge=1)


class ApprovedOut(BaseModel):
    approved: int


class RunListItem(BaseModel):
    id: uuid.UUID
    questionnaire_id: uuid.UUID
    filename: str
    status: Literal["running", "finished", "cancelled"]
    created_at: datetime
    finished_at: datetime | None
    # How many of its answers wait again because a live setting changed.
    reopened: int = 0


class RunList(BaseModel):
    items: list[RunListItem]


async def _question_out(
    session: SessionDep, tenant_id: uuid.UUID, item: RunQuestion, warnings: list[str]
) -> ReviewQuestionOut:
    text = (
        await session.execute(
            select(Question.text).where(
                Question.tenant_id == tenant_id, Question.id == item.question_id
            )
        )
    ).scalar_one()
    cited = item.citations or []
    resolved = await sources.resolve_all(session, tenant_id, cited)
    latest = (
        await session.execute(
            select(DraftCheck)
            .where(DraftCheck.tenant_id == tenant_id, DraftCheck.run_question_id == item.id)
            .order_by(DraftCheck.checked_at.desc(), DraftCheck.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ReviewQuestionOut(
        id=item.id,
        run_id=item.run_id,
        position=item.position,
        question=text,
        status=item.status,  # type: ignore[arg-type]
        path=item.path,  # type: ignore[arg-type]
        reason=item.reason,
        answer=item.answer,
        note=item.note,
        sources=[
            SourceOut(id=source_id, available=False)
            if found is None
            else SourceOut(
                id=source_id,
                available=True,
                kind=found.kind,
                text=found.text,
                label=found.label,
                owner=found.owner,
                valid_until=found.valid_until,
                observed_at=found.observed_at,
            )
            for source_id, found in zip(cited, resolved, strict=True)
        ],
        review=item.review,  # type: ignore[arg-type]
        final_answer=item.final_answer,
        version=item.version,
        redrafts=item.redrafts,
        steps=item.steps,
        cost_usd=item.cost_usd,
        warnings=warnings,
        check=None
        if latest is None
        else DraftCheckOut(
            outcome=latest.outcome,  # type: ignore[arg-type]
            findings=[FindingOut(**found) for found in latest.findings],
            model=latest.model,
            on_current_answer=latest.answer_checked == (item.final_answer or item.answer),
        ),
        reopening=reopening_out(await reopening_of(session, item)),
    )


@router.get("/runs")
async def list_runs(
    session: SessionDep, tenant_id: TenantDep, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> RunList:
    """The newest runs first."""
    reopened = (
        select(func.count())
        .where(RunQuestion.run_id == Run.id, RunQuestion.reopened_at.is_not(None))
        .scalar_subquery()
    )
    rows = await session.execute(
        select(Run, Questionnaire.filename, reopened)
        .join(Questionnaire, Questionnaire.id == Run.questionnaire_id)
        .where(Run.tenant_id == tenant_id)
        .order_by(Run.created_at.desc())
        .limit(limit)
    )
    return RunList(
        items=[
            RunListItem(
                id=run.id,
                questionnaire_id=run.questionnaire_id,
                filename=filename,
                status=run.status,
                created_at=run.created_at,
                finished_at=run.finished_at,
                reopened=count,
            )
            for run, filename, count in rows
        ]
    )


@router.get("/run-questions/{item_id}", responses={404: {"description": "No such question."}})
async def get_run_question(
    item_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep
) -> ReviewQuestionOut:
    item = (
        await session.execute(
            select(RunQuestion).where(RunQuestion.tenant_id == tenant_id, RunQuestion.id == item_id)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")
    return await _question_out(session, tenant_id, item, [])


@router.post(
    "/run-questions/{item_id}/decision",
    responses={
        404: {"description": "No such question."},
        409: {"description": "The question changed, or is not waiting for a decision."},
        422: {"description": "The decision is missing something it needs."},
    },
)
async def decide(
    item_id: uuid.UUID,
    body: DecisionIn,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> ReviewQuestionOut:
    """Approve, edit, reject, or send back for a redraft. Recorded with who and when."""
    try:
        decided = await review.decide(
            session,
            tenant_id=tenant_id,
            item_id=item_id,
            decision=body.decision,
            version=body.version,
            answer=body.answer,
            comment=body.comment,
            reviewer=settings.dev_reviewer,
            max_redrafts=settings.max_redrafts,
            max_chars=settings.library_text_max_chars,
        )
    except review.ReviewConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except review.InvalidDecisionError as error:
        raise HTTPException(_UNPROCESSABLE, str(error)) from error
    if decided is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")

    item = decided.item
    if item.review == REVIEW_REDRAFTING:
        # The decision is committed. This only wakes a worker; the sweep covers a miss.
        try:
            queue = JobQueue(redis, stream=settings.queue_stream, consumer="api")
            await queue.add([str(item.id)])
        except (RedisError, OSError) as error:
            log.warning("redraft of %s not queued yet: %s", item.id, type(error).__name__)
    await publish_changed(redis, item.run_id)
    return await _question_out(session, tenant_id, item, decided.warnings)


@router.post(
    "/runs/{run_id}/approve-ready",
    responses={
        404: {"description": "No such run."},
        409: {"description": "The number of ready questions is not what was shown."},
    },
)
async def approve_ready(
    run_id: uuid.UUID,
    body: ApproveReadyIn,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> ApprovedOut:
    """Approve every question that passed all checks and is still waiting. Each one gets
    its own decision record, marked as approved in a batch."""
    try:
        approved = await review.approve_ready(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            count=body.count,
            reviewer=settings.dev_reviewer,
        )
    except review.ReviewConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if approved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    await publish_changed(redis, run_id)
    return ApprovedOut(approved=approved)


@router.get("/sources/{source_id}", responses={404: {"description": "No such source."}})
async def get_source(source_id: str, session: SessionDep, tenant_id: TenantDep) -> SourceOut:
    """The text behind a citation: an approved answer, a policy section, or evidence."""
    found = await sources.resolve(session, tenant_id, source_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found.")
    return SourceOut(
        id=found.id,
        available=True,
        kind=found.kind,
        text=found.text,
        label=found.label,
        owner=found.owner,
        valid_until=found.valid_until,
        observed_at=found.observed_at,
    )
