"""What a reviewer is shown about a change: read-only, and always for one tenant."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.drift.compare import Change, changed_facts
from standingproof.drift.watch import LIVE_REVIEWS, LIVE_STATUSES, STOPPED
from standingproof.models import (
    AnswerEvidence,
    AnswerReopening,
    DriftEvent,
    Evidence,
    Question,
    Questionnaire,
    Run,
    RunQuestion,
    SentFile,
)


@dataclass(frozen=True)
class Observation:
    # Built by our own code from allow-listed facts; no text from the remote system.
    statement: str
    observed_at: datetime


@dataclass(frozen=True)
class SettingChange:
    check_name: str
    scope: str
    was: Observation | None
    now: Observation
    # Empty when the setting changed and changed back.
    changes: list[Change]
    trigger: str


@dataclass(frozen=True)
class SentTo:
    customer: str
    sent_at: datetime


@dataclass(frozen=True)
class Reopening:
    reopened_at: datetime
    # approved | edited, or None for a draft nobody had decided.
    was_review: str | None
    settings: list[SettingChange]
    sent_to: list[SentTo]


@dataclass(frozen=True)
class ReopenedAnswer:
    run_question_id: uuid.UUID
    run_id: uuid.UUID
    filename: str
    position: int
    question: str
    # True while nobody has decided it again.
    waiting: bool
    sent_to: list[SentTo]


@dataclass(frozen=True)
class ChangeEntry:
    id: uuid.UUID
    detected_at: datetime
    change: SettingChange
    answers: list[ReopenedAnswer]


@dataclass(frozen=True)
class Watched:
    check_name: str
    scope: str
    answers: int
    last_status: str
    last_looked_at: datetime
    last_observed_at: datetime | None


def _seen(evidence: Evidence | None) -> Observation | None:
    return None if evidence is None else Observation(evidence.statement, evidence.observed_at)


def _change(
    was: Evidence | None, now: Evidence, trigger: str, check_name: str, scope: str
) -> SettingChange:
    changes = changed_facts(was.facts, now.facts) if was is not None else []
    return SettingChange(
        check_name, scope, _seen(was), Observation(now.statement, now.observed_at), changes, trigger
    )


async def _evidence(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Evidence]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Evidence).where(Evidence.tenant_id == tenant_id, Evidence.id.in_(ids))
    )
    return {row.id: row for row in rows.scalars()}


async def sent_to(session: AsyncSession, item: RunQuestion) -> list[SentTo]:
    """The customers a file with this answer's approved wording was recorded as sent to."""
    if not item.final_answer:
        return []
    wording = hashlib.sha256(item.final_answer.encode()).hexdigest()
    rows = await session.execute(
        select(SentFile.customer, SentFile.sent_at, SentFile.answers)
        .where(SentFile.tenant_id == item.tenant_id, SentFile.run_id == item.run_id)
        .order_by(SentFile.seq)
    )
    question = str(item.question_id)
    return [
        SentTo(customer, sent_at)
        for customer, sent_at, answers in rows
        if any(a["question_id"] == question and a["wording_sha256"] == wording for a in answers)
    ]


async def reopening_of(session: AsyncSession, item: RunQuestion) -> Reopening | None:
    """What put this question back in front of a reviewer. None if nothing has."""
    if item.reopened_at is None:
        return None
    rows = (
        await session.execute(
            select(AnswerReopening, DriftEvent)
            .join(DriftEvent, DriftEvent.id == AnswerReopening.drift_event_id)
            .where(
                AnswerReopening.tenant_id == item.tenant_id,
                AnswerReopening.run_question_id == item.id,
                # Since the last decision, by stored order: a reopening stamped by a clock
                # that was stepped back is still one of them.
                AnswerReopening.seq >= item.reopened_seq,
            )
            .order_by(AnswerReopening.seq)
        )
    ).all()
    if not rows:
        return None
    # Per setting: what the answer rested on at first, and the newest reading since.
    first: dict[tuple[str, str], AnswerReopening] = {}
    last: dict[tuple[str, str], DriftEvent] = {}
    for reopening, event in rows:
        setting = (event.check_name, event.scope)
        first.setdefault(setting, reopening)
        last[setting] = event
    wanted = {r.was_evidence_id for r in first.values()} | {e.evidence_id for e in last.values()}
    evidence = await _evidence(session, item.tenant_id, wanted)
    return Reopening(
        reopened_at=item.reopened_at,
        was_review=rows[0][0].was_review,
        settings=[
            _change(
                evidence.get(first[setting].was_evidence_id),
                evidence[event.evidence_id],
                event.trigger,
                *setting,
            )
            for setting, event in last.items()
        ],
        sent_to=await sent_to(session, item),
    )


async def changes_page(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int
) -> list[ChangeEntry]:
    """The newest changes seen to this tenant's settings, with the answers each reopened."""
    events = list(
        (
            await session.execute(
                select(DriftEvent)
                .join(Evidence, Evidence.id == DriftEvent.evidence_id)
                .where(DriftEvent.tenant_id == tenant_id)
                # By the order the observations were stored in, not by `detected_at`: a
                # clock that is stepped back would list an older change above a newer one.
                .order_by(Evidence.seq.desc())
                .limit(limit)
            )
        ).scalars()
    )
    reopened = (
        await session.execute(
            select(AnswerReopening, RunQuestion, Question.text, Questionnaire.filename)
            .join(RunQuestion, RunQuestion.id == AnswerReopening.run_question_id)
            .join(Question, Question.id == RunQuestion.question_id)
            .join(Run, Run.id == RunQuestion.run_id)
            .join(Questionnaire, Questionnaire.id == Run.questionnaire_id)
            .where(
                AnswerReopening.tenant_id == tenant_id,
                AnswerReopening.drift_event_id.in_([event.id for event in events]),
            )
            .order_by(Run.created_at.desc(), Run.id, RunQuestion.position)
        )
    ).all()
    by_event: dict[uuid.UUID, list[tuple[AnswerReopening, RunQuestion, str, str]]] = {}
    for reopening, item, text, filename in reopened:
        by_event.setdefault(reopening.drift_event_id, []).append((reopening, item, text, filename))
    wanted: set[uuid.UUID] = set()
    for event in events:
        wanted.add(event.evidence_id)
        # Compared with the reading before it, or, when that one read the same, with what
        # the first answer it reopened had rested on.
        answers = by_event.get(event.id, [])
        before = event.previous_evidence_id or (answers[0][0].was_evidence_id if answers else None)
        if before is not None:
            wanted.add(before)
    evidence = await _evidence(session, tenant_id, wanted)

    entries = []
    for event in events:
        answers = by_event.get(event.id, [])
        before = event.previous_evidence_id or (answers[0][0].was_evidence_id if answers else None)
        entries.append(
            ChangeEntry(
                id=event.id,
                detected_at=event.detected_at,
                change=_change(
                    evidence.get(before) if before else None,
                    evidence[event.evidence_id],
                    event.trigger,
                    event.check_name,
                    event.scope,
                ),
                answers=[
                    ReopenedAnswer(
                        run_question_id=item.id,
                        run_id=item.run_id,
                        filename=filename,
                        position=item.position,
                        question=text,
                        waiting=item.reopened_at is not None,
                        sent_to=await sent_to(session, item),
                    )
                    for _, item, text, filename in answers
                ],
            )
        )
    return entries


async def watched(session: AsyncSession, tenant_id: uuid.UUID) -> list[Watched]:
    """Each setting a live answer rests on, with how the last look at it went."""
    settings = (
        await session.execute(
            select(AnswerEvidence.check_name, AnswerEvidence.scope, func.count())
            .join(RunQuestion, RunQuestion.id == AnswerEvidence.run_question_id)
            .where(
                AnswerEvidence.tenant_id == tenant_id,
                RunQuestion.status.in_(LIVE_STATUSES),
                RunQuestion.review.in_(LIVE_REVIEWS),
            )
            .group_by(AnswerEvidence.check_name, AnswerEvidence.scope)
            .order_by(AnswerEvidence.check_name, AnswerEvidence.scope)
        )
    ).all()
    found = []
    for check_name, scope, answers in settings:
        looks = (
            select(Evidence.status, Evidence.observed_at)
            .where(
                Evidence.tenant_id == tenant_id,
                Evidence.check_name == check_name,
                Evidence.scope == scope,
                # A look the stop switch refused looked at nothing.
                Evidence.reason.is_distinct_from(STOPPED),
            )
            .order_by(Evidence.seq.desc())
            .limit(1)
        )
        last = (await session.execute(looks)).one()
        seen = (await session.execute(looks.where(Evidence.status == "observed"))).one_or_none()
        found.append(
            Watched(check_name, scope, answers, last[0], last[1], seen[1] if seen else None)
        )
    return found
