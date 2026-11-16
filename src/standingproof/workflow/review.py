"""A person's decision about a question. The only code that can approve anything.

A decision is one append-only row, written in the same transaction as the question's
review state. Nothing here calls a model or touches the graph: a redraft only puts the
question back on the queue, and a worker takes it from there.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.agent.claims import unsupported_claims
from standingproof.library.sources import resolve_all
from standingproof.models import (
    ITEM_QUEUED,
    ITEM_READY,
    REVIEW_APPROVED,
    REVIEW_EDITED,
    REVIEW_PENDING,
    REVIEW_REDRAFTING,
    REVIEW_REJECTED,
    RUN_FINISHED,
    RUN_RUNNING,
    AnswerReopening,
    ReviewDecision,
    Run,
    RunQuestion,
)

Decision = Literal["approve", "edit", "reject", "redraft", "keep"]

_NOW = func.now()


class ReviewConflictError(Exception):
    """The decision cannot be applied to the question as it is now. Maps to 409."""


class InvalidDecisionError(Exception):
    """The decision is missing something it needs. Maps to 422."""


@dataclass(frozen=True)
class Decided:
    item: RunQuestion
    # Strong terms in an edited answer that none of its sources use. Shown, not enforced.
    warnings: list[str]


async def _apply(
    session: AsyncSession,
    item: RunQuestion,
    *,
    decision: Decision,
    final_answer: str | None,
    comment: str | None,
    reviewer: str,
    batch: bool,
    # For `keep`: the review state to put back, and the wording that was on the screen.
    restore: str | None = None,
    shown: str | None = None,
) -> None:
    session.add(
        ReviewDecision(
            tenant_id=item.tenant_id,
            run_question_id=item.id,
            decision=decision,
            shown_answer=shown if shown is not None else item.answer,
            final_answer=final_answer,
            comment=comment,
            batch=batch,
            decided_by=reviewer,
        )
    )
    item.review = (
        restore
        or {
            "approve": REVIEW_APPROVED,
            "edit": REVIEW_EDITED,
            "reject": REVIEW_REJECTED,
            "redraft": REVIEW_REDRAFTING,
            "keep": REVIEW_APPROVED,
        }[decision]
    )
    item.final_answer = final_answer
    item.version += 1
    item.reviewed_at = _NOW
    # Whatever was decided, a person has now looked at the change.
    item.reopened_at = None


async def _review_before_reopening(session: AsyncSession, item: RunQuestion) -> str | None:
    """What the question's review state was when it was reopened: `approved`, `edited`,
    or None for a draft nobody had decided. The first reopening since `reopened_at`."""
    return (
        await session.execute(
            select(AnswerReopening.was_review)
            .where(
                AnswerReopening.tenant_id == item.tenant_id,
                AnswerReopening.run_question_id == item.id,
                AnswerReopening.reopened_at >= item.reopened_at,
            )
            .order_by(AnswerReopening.reopened_at, AnswerReopening.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _reopen_run(session: AsyncSession, run_id: uuid.UUID) -> None:
    """A redraft is work, so its run is running again."""
    try:
        async with session.begin_nested():
            await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RUN_FINISHED)
                .values(status=RUN_RUNNING, finished_at=None)
            )
    except IntegrityError:
        # Another run of this questionnaire has started since. Two cannot be active.
        raise ReviewConflictError(
            "a newer run of this questionnaire is in progress; redraft there"
        ) from None


async def decide(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    item_id: uuid.UUID,
    decision: Decision,
    version: int,
    reviewer: str,
    answer: str | None = None,
    comment: str | None = None,
    max_redrafts: int,
    max_chars: int,
) -> Decided | None:
    """Record one decision. None if the question is not this tenant's."""
    item = (
        await session.execute(
            select(RunQuestion)
            .where(RunQuestion.tenant_id == tenant_id, RunQuestion.id == item_id)
            # Two reviewers deciding at once take turns; the second then sees a new version.
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        return None
    if item.review != REVIEW_PENDING:
        raise ReviewConflictError("this question is not waiting for a decision")
    if item.version != version:
        raise ReviewConflictError("this question has changed since you opened it")

    answer = (answer or "").strip()
    comment = (comment or "").strip() or None
    warnings: list[str] = []
    final_answer: str | None = None
    restore: str | None = None
    shown: str | None = None
    reopened = item.reopened_at is not None

    if decision == "keep":
        if not reopened:
            raise ReviewConflictError("this question was not reopened; approve it instead")
        restore = await _review_before_reopening(session, item)
        # An answer that had been decided keeps its approved wording, which for an edit is
        # the reviewer's and not the agent's draft. A draft nobody had decided is approved.
        final_answer = item.final_answer if restore is not None else item.answer
        if not (final_answer or "").strip():
            raise InvalidDecisionError("there is no answer to keep; write one instead")
        shown = final_answer
    elif decision == "approve":
        if reopened:
            # Approve means "the agent's draft is right". Here the question is whether an
            # answer still stands after a change, which may not be the agent's draft.
            raise ReviewConflictError(
                "a setting this answer rests on has changed; keep it, edit it or withdraw it"
            )
        if not (item.answer or "").strip():
            raise InvalidDecisionError("there is no draft to approve; write an answer instead")
        final_answer = item.answer
    elif decision == "edit":
        if not answer:
            raise InvalidDecisionError("an edited answer cannot be empty")
        if len(answer) > max_chars:
            raise InvalidDecisionError(f"an answer may be at most {max_chars} characters")
        final_answer = answer
        sources = await resolve_all(session, tenant_id, item.citations or [])
        warnings = unsupported_claims(answer, [s.text for s in sources if s is not None])
    elif decision == "redraft":
        if comment is None:
            raise InvalidDecisionError("say what the new draft should do differently")
        if len(comment) > max_chars:
            raise InvalidDecisionError(f"an instruction may be at most {max_chars} characters")
        if item.redrafts >= max_redrafts:
            raise ReviewConflictError(
                f"this question has already been redrafted {max_redrafts} times; edit it instead"
            )
        await _reopen_run(session, item.run_id)
        item.status = ITEM_QUEUED
        item.redrafts += 1
        item.attempts = 0
        item.finished_at = None
        item.enqueue_after = _NOW

    await _apply(
        session,
        item,
        decision=decision,
        final_answer=final_answer,
        comment=comment,
        reviewer=reviewer,
        batch=False,
        restore=restore,
        shown=shown,
    )
    await session.commit()
    await session.refresh(item)
    return Decided(item=item, warnings=warnings)


async def approve_ready(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, count: int, reviewer: str
) -> int | None:
    """Approve every ready question of a run that is still waiting. None if not our run.

    `count` is how many the reviewer was shown. If that is no longer the number, nothing is
    approved: they would be approving something they did not see.
    """
    run = (
        await session.execute(select(Run.id).where(Run.tenant_id == tenant_id, Run.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return None
    items = (
        (
            await session.execute(
                select(RunQuestion)
                .where(
                    RunQuestion.tenant_id == tenant_id,
                    RunQuestion.run_id == run_id,
                    RunQuestion.status == ITEM_READY,
                    RunQuestion.review == REVIEW_PENDING,
                    # A setting it rests on has changed: it must be opened and read.
                    RunQuestion.reopened_at.is_(None),
                )
                .order_by(RunQuestion.position)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if len(items) != count:
        raise ReviewConflictError(
            f"{len(items)} questions are ready now, not {count}; look again before approving"
        )
    for item in items:
        await _apply(
            session,
            item,
            decision="approve",
            final_answer=item.answer,
            comment=None,
            reviewer=reviewer,
            batch=True,
        )
    await session.commit()
    return len(items)
