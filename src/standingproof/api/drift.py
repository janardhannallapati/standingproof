"""What changed in a live setting, and which answers that put back in front of a person."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from standingproof.api.deps import SessionDep, TenantDep
from standingproof.drift import view

router = APIRouter(tags=["drift"])

# A named fact of a check: a flag, a count or a short name our own code produced.
Fact = bool | int | float | str | None


class ChangedFactOut(BaseModel):
    fact: str
    was: Fact
    now: Fact


class ObservationOut(BaseModel):
    statement: str
    observed_at: datetime


class SettingChangeOut(BaseModel):
    check_name: str
    scope: str
    # None only when the earlier observation can no longer be found.
    was: ObservationOut | None
    now: ObservationOut
    # Empty when the setting changed and then changed back.
    changes: list[ChangedFactOut]
    # Why the setting was looked at: run | schedule | webhook | manual
    trigger: str


class SentToOut(BaseModel):
    customer: str
    sent_at: datetime


class ReopeningOut(BaseModel):
    """Why a question is waiting for a decision again."""

    reopened_at: datetime
    # What it was before: approved or edited. None for a draft nobody had decided.
    was_review: str | None
    settings: list[SettingChangeOut]
    # Customers a file with this wording was recorded as sent to.
    sent_to: list[SentToOut]


class ReopenedAnswerOut(BaseModel):
    run_question_id: uuid.UUID
    run_id: uuid.UUID
    filename: str
    position: int
    question: str
    # True while nobody has decided it again.
    waiting: bool
    sent_to: list[SentToOut]


class ChangeOut(SettingChangeOut):
    id: uuid.UUID
    detected_at: datetime
    answers: list[ReopenedAnswerOut]


class ChangeList(BaseModel):
    items: list[ChangeOut]


class WatchedOut(BaseModel):
    check_name: str
    scope: str
    # How many answers rest on it.
    answers: int
    # How the last look went: observed | unavailable | not_configured | denied
    last_status: str
    last_looked_at: datetime
    last_observed_at: datetime | None


class WatchedList(BaseModel):
    items: list[WatchedOut]


def _sent(sent: list[view.SentTo]) -> list[SentToOut]:
    return [SentToOut(customer=each.customer, sent_at=each.sent_at) for each in sent]


def _setting(change: view.SettingChange) -> SettingChangeOut:
    return SettingChangeOut(
        check_name=change.check_name,
        scope=change.scope,
        was=ObservationOut(statement=change.was.statement, observed_at=change.was.observed_at)
        if change.was
        else None,
        now=ObservationOut(statement=change.now.statement, observed_at=change.now.observed_at),
        changes=[ChangedFactOut(fact=c.fact, was=c.was, now=c.now) for c in change.changes],
        trigger=change.trigger,
    )


def reopening_out(reopening: view.Reopening | None) -> ReopeningOut | None:
    if reopening is None:
        return None
    return ReopeningOut(
        reopened_at=reopening.reopened_at,
        was_review=reopening.was_review,
        settings=[_setting(change) for change in reopening.settings],
        sent_to=_sent(reopening.sent_to),
    )


@router.get("/changes")
async def list_changes(
    session: SessionDep, tenant_id: TenantDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> ChangeList:
    """The newest changes seen to live settings, with the answers each one reopened.

    Read-only: an answer is decided on its own question, one at a time. The statements are
    built by this product from named facts; no text from the remote system is in them.
    """
    entries = await view.changes_page(session, tenant_id, limit=limit)
    return ChangeList(
        items=[
            ChangeOut(
                **_setting(entry.change).model_dump(),
                id=entry.id,
                detected_at=entry.detected_at,
                answers=[
                    ReopenedAnswerOut(
                        run_question_id=answer.run_question_id,
                        run_id=answer.run_id,
                        filename=answer.filename,
                        position=answer.position,
                        question=answer.question,
                        waiting=answer.waiting,
                        sent_to=_sent(answer.sent_to),
                    )
                    for answer in entry.answers
                ],
            )
            for entry in entries
        ]
    )


@router.get("/checks/watched")
async def list_watched(session: SessionDep, tenant_id: TenantDep) -> WatchedList:
    """The settings answers rest on, and how the last look at each went."""
    return WatchedList(
        items=[
            WatchedOut(
                check_name=each.check_name,
                scope=each.scope,
                answers=each.answers,
                last_status=each.last_status,
                last_looked_at=each.last_looked_at,
                last_observed_at=each.last_observed_at,
            )
            for each in await view.watched(session, tenant_id)
        ]
    )
