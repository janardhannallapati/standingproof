"""Compare a new observation of a setting with every answer that rests on that setting.

An answer whose facts differ is put back in front of a reviewer. Plain code: no model reads
or decides anything here. The work for one setting is one transaction, taken under a lock
on that setting, so it is correct when two processes do it at once, when it is repeated,
and when the process dies in the middle.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.audit import Entry, record
from standingproof.db import Database
from standingproof.drift.compare import Change, changed_facts
from standingproof.drift.watch import LIVE_REVIEWS, LIVE_STATUSES
from standingproof.models import (
    REVIEW_PENDING,
    AnswerEvidence,
    AnswerReopening,
    DriftEvent,
    DriftQueue,
    Evidence,
    RunQuestion,
)


async def _lock_setting(session: AsyncSession, evidence: Evidence) -> None:
    """One examiner per setting at a time, until this transaction ends."""
    key = f"drift:{evidence.tenant_id}:{evidence.check_name}:{evidence.scope}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def _newest_two(session: AsyncSession, of: Evidence) -> list[Evidence]:
    rows = await session.execute(
        select(Evidence)
        .where(
            Evidence.tenant_id == of.tenant_id,
            Evidence.check_name == of.check_name,
            Evidence.scope == of.scope,
            Evidence.status == "observed",
        )
        .order_by(Evidence.seq.desc())
        .limit(2)
    )
    return list(rows.scalars())


async def _event(
    session: AsyncSession, latest: Evidence, previous: Evidence | None, trigger: str
) -> uuid.UUID:
    """The event for this observation: made now, or found if it was made before."""
    made = (
        await session.execute(
            insert(DriftEvent)
            .values(
                tenant_id=latest.tenant_id,
                check_name=latest.check_name,
                scope=latest.scope,
                evidence_id=latest.id,
                previous_evidence_id=previous.id if previous is not None else None,
                trigger=trigger,
            )
            .on_conflict_do_nothing(index_elements=[DriftEvent.evidence_id])
            .returning(DriftEvent.id)
        )
    ).scalar_one_or_none()
    if made is not None:
        return made
    return (
        await session.execute(select(DriftEvent.id).where(DriftEvent.evidence_id == latest.id))
    ).scalar_one()


async def _reopen(
    session: AsyncSession,
    item: RunQuestion,
    link: AnswerEvidence,
    *,
    event_id: uuid.UUID,
    latest_id: uuid.UUID,
    changes: list[Change],
) -> None:
    waiting = item.review == REVIEW_PENDING
    made = await session.execute(
        insert(AnswerReopening)
        .values(
            tenant_id=item.tenant_id,
            drift_event_id=event_id,
            run_question_id=item.id,
            was_evidence_id=link.evidence_id,
            changes=[{"fact": c.fact, "was": c.was, "now": c.now} for c in changes],
            # What "keep" puts back. A question already waiting has nothing to put back.
            was_review=None if waiting else item.review,
        )
        .on_conflict_do_nothing(constraint="uq_answer_reopenings_once")
        .returning(AnswerReopening.seq)
    )
    seq = made.scalar_one_or_none()
    if seq is None:
        # This event already reopened this answer (a second pass over the same change).
        seq = (
            await session.execute(
                select(AnswerReopening.seq).where(
                    AnswerReopening.drift_event_id == event_id,
                    AnswerReopening.run_question_id == item.id,
                )
            )
        ).scalar_one()
    await record(
        session,
        item.tenant_id,
        Entry(
            action="answer.reopen",
            subject=f"run_question:{item.id}",
            details={"drift_event_id": str(event_id), "facts_changed": len(changes)},
            actor="watcher",
        ),
    )
    # Back to a person. `status`, `reason` and the approved wording are not touched: they
    # say what the agent concluded and what was approved, and both are still true.
    item.review = REVIEW_PENDING
    if item.reopened_at is None:
        item.reopened_at = func.now()
        # Which reopenings belong to this wait: this one and every later one.
        item.reopened_seq = seq
    # An open screen holds the old version; its decision is refused and it reloads.
    item.version += 1
    # From now on the answer is measured against what the reviewer is being shown.
    link.evidence_id = latest_id


async def examine_next(session: AsyncSession) -> list[uuid.UUID] | None:
    """Examine the setting of one queued observation. None when nothing is queued.

    Returns the runs in which an answer was reopened. Commits.
    """
    taken = (
        await session.execute(
            select(DriftQueue, Evidence)
            .join(Evidence, Evidence.id == DriftQueue.evidence_id)
            # The observation stored first, by its number.
            .order_by(Evidence.seq)
            .limit(1)
            # Two examiners each take a different row.
            .with_for_update(of=DriftQueue, skip_locked=True)
        )
    ).one_or_none()
    if taken is None:
        return None
    queued, observed = taken
    await _lock_setting(session, observed)

    # Always the newest reading of the setting, whichever observation woke us. So the
    # order in which queued rows are examined does not matter.
    newest = await _newest_two(session, observed)
    latest = newest[0]
    previous = newest[1] if len(newest) > 1 else None
    if previous is not None and not changed_facts(previous.facts, latest.facts):
        previous = None

    resting = (
        await session.execute(
            select(AnswerEvidence, RunQuestion, Evidence.facts)
            .join(RunQuestion, RunQuestion.id == AnswerEvidence.run_question_id)
            .join(Evidence, Evidence.id == AnswerEvidence.evidence_id)
            .where(
                AnswerEvidence.tenant_id == latest.tenant_id,
                AnswerEvidence.check_name == latest.check_name,
                AnswerEvidence.scope == latest.scope,
                AnswerEvidence.evidence_id != latest.id,
                RunQuestion.status.in_(LIVE_STATUSES),
                RunQuestion.review.in_(LIVE_REVIEWS),
            )
            # The order "approve all ready" locks in, so the two cannot deadlock.
            .order_by(RunQuestion.run_id, RunQuestion.position)
            .with_for_update(of=RunQuestion)
        )
    ).all()
    differing: list[tuple[AnswerEvidence, RunQuestion, list[Change]]] = []
    for link, item, facts in resting:
        changes = changed_facts(facts, latest.facts)
        if changes:
            differing.append((link, item, changes))

    runs: list[uuid.UUID] = []
    if differing or previous is not None:
        # Why the newest observation was made, which may not be the row that woke us.
        trigger = (
            await session.execute(
                select(DriftQueue.trigger).where(DriftQueue.evidence_id == latest.id)
            )
        ).scalar_one_or_none()
        event_id = await _event(session, latest, previous, trigger or queued.trigger)
        for link, item, changes in differing:
            await _reopen(
                session, item, link, event_id=event_id, latest_id=latest.id, changes=changes
            )
            if item.run_id not in runs:
                runs.append(item.run_id)
    # Removed by a function: the app role holds DELETE on no table (it would also be the
    # right to lock the table). The function takes out this tenant's row and no other.
    await session.execute(select(func.drop_from_drift_queue(queued.evidence_id)))
    await session.commit()
    return runs


async def examine_pending(database: Database, *, limit: int) -> list[uuid.UUID]:
    """Examine up to `limit` queued observations. Returns the runs with a reopened answer.

    Which tenants have something queued is asked across tenants. Each observation is then
    examined as its own tenant, so the comparison can read and reopen that tenant's
    answers and nobody else's.
    """
    async with database.across_tenants() as session:
        waiting = await session.execute(
            select(DriftQueue.tenant_id)
            .join(Evidence, Evidence.id == DriftQueue.evidence_id)
            .group_by(DriftQueue.tenant_id)
            # The tenant whose oldest waiting observation was stored first.
            .order_by(func.min(Evidence.seq))
        )
        tenants = [row[0] for row in waiting]
    runs: list[uuid.UUID] = []
    examined = 0
    for tenant_id in tenants:
        while examined < limit:
            async with database.tenant(tenant_id)() as session:
                reopened = await examine_next(session)
            if reopened is None:
                break
            examined += 1
            runs.extend(run for run in reopened if run not in runs)
    return runs
