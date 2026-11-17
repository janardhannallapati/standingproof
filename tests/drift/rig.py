"""Rows the drift tests start from: observations of a setting, and answers resting on them."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select

from standingproof.drift import examine
from standingproof.models import (
    AnswerEvidence,
    AnswerReopening,
    DriftEvent,
    DriftQueue,
    Evidence,
    RunQuestion,
)
from tests.conftest import Db
from tests.workflow.seed import questionnaire, start, tenant
from tests.workflow.test_review import decide, drafted

CHECK = "github.branch_protection"
SCOPE = "acme/app"
PROTECTED = {"default_branch": "main", "protected": True, "required_approving_reviews": 2}
OPEN = {"default_branch": "main", "protected": False, "required_approving_reviews": 0}


def observe(
    db: Db,
    tenant_id: uuid.UUID,
    facts: dict[str, Any] | None = None,
    *,
    check: str = CHECK,
    scope: str = SCOPE,
    status: str = "observed",
    at: dt.datetime | None = None,
    queue: bool = True,
    trigger: str = "schedule",
) -> uuid.UUID:
    """Store one look at a setting, as `run_check` would leave it."""

    async def go() -> uuid.UUID:
        async with db.sessions() as session:
            seen = status == "observed"
            row = Evidence(
                tenant_id=tenant_id,
                check_name=check,
                scope=scope,
                status=status,
                facts=(facts if facts is not None else PROTECTED) if seen else {},
                raw_result={},
                statement=f"{check} on {scope}: {facts if facts is not None else PROTECTED}",
                reason=None if seen else "unreachable",
                observed_at=at or dt.datetime.now(dt.UTC),
            )
            session.add(row)
            await session.flush()
            if queue and seen:
                session.add(DriftQueue(evidence_id=row.id, tenant_id=tenant_id, trigger=trigger))
            await session.commit()
            return row.id

    return db.run(go())


def answers(
    db: Db, tenant_id: uuid.UUID, evidence_id: uuid.UUID, count: int = 1
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A run with `count` drafted questions that each cite the observation. Version 1."""
    started = start(db, tenant_id, questionnaire(db, tenant_id, [f"Q{n}?" for n in range(count)]))
    for item in started.item_ids:
        drafted(db, item, answer="Yes, two reviews.", citations=[f"evidence:{evidence_id}"])
    return started.run.id, list(started.item_ids)


def approved_answer(db: Db, name: str = "default") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, and one approved answer resting on a first observation."""
    owner = tenant(db, name)
    first = observe(db, owner, queue=False)
    _, [item] = answers(db, owner, first)
    decide(db, owner, item, "approve")
    return owner, item, first


def examine_all(db: Db) -> list[uuid.UUID]:
    """Examine everything queued. Returns the runs with a reopened answer."""

    async def go() -> list[uuid.UUID]:
        return await examine.examine_pending(db.database, limit=50)

    return db.run(go())


def item_row(db: Db, item: uuid.UUID) -> RunQuestion:
    async def go() -> RunQuestion:
        async with db.sessions() as session:
            return (
                await session.execute(select(RunQuestion).where(RunQuestion.id == item))
            ).scalar_one()

    return db.run(go())


def all_rows(db: Db, model: Any) -> list[Any]:
    async def go() -> list[Any]:
        async with db.sessions() as session:
            return list((await session.execute(select(model))).scalars())

    return db.run(go())


def events(db: Db) -> list[DriftEvent]:
    return all_rows(db, DriftEvent)


def reopenings(db: Db) -> list[AnswerReopening]:
    return sorted(all_rows(db, AnswerReopening), key=lambda row: row.reopened_at)


def rests_on(db: Db, item: uuid.UUID) -> list[tuple[str, str, uuid.UUID]]:
    return [
        (row.check_name, row.scope, row.evidence_id)
        for row in all_rows(db, AnswerEvidence)
        if row.run_question_id == item
    ]


def queued(db: Db) -> int:
    return len(all_rows(db, DriftQueue))
