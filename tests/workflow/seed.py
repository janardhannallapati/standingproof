"""Rows the workflow tests start from, written straight to the database."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from standingproof.db import build_engine
from standingproof.models import (
    STATUS_PARSED,
    LibraryAnswer,
    Question,
    Questionnaire,
    RunQuestion,
    Tenant,
)
from standingproof.workflow.runs import StartedRun, start_run
from tests.conftest import EMPTY_THE_DATABASE, Db


async def _truncate(database_url: str) -> None:
    engine = build_engine(database_url)
    try:
        async with engine.begin() as conn:
            for statement in EMPTY_THE_DATABASE:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def wf(migrated_postgres_url: str) -> Iterator[Db]:
    """An empty, migrated database with one loop and pool for the test."""
    asyncio.run(_truncate(migrated_postgres_url))
    database = Db(migrated_postgres_url)
    yield database
    database.close()


def tenant(db: Db, name: str = "default") -> uuid.UUID:
    async def create() -> uuid.UUID:
        async with db.sessions() as session:
            row = Tenant(name=name)
            session.add(row)
            await session.commit()
            return row.id

    return db.run(create())


def questionnaire(
    db: Db, tenant_id: uuid.UUID, questions: list[str], status: str = STATUS_PARSED
) -> uuid.UUID:
    async def create() -> uuid.UUID:
        async with db.sessions() as session:
            row = Questionnaire(
                tenant_id=tenant_id,
                filename="q.xlsx",
                status=status,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                question_count=len(questions),
            )
            session.add(row)
            await session.flush()
            for position, wording in enumerate(questions, start=1):
                session.add(
                    Question(
                        tenant_id=tenant_id,
                        questionnaire_id=row.id,
                        position=position,
                        sheet="Sheet1",
                        cell=f"A{position + 1}",
                        text=wording,
                    )
                )
            await session.commit()
            return row.id

    return db.run(create())


def approved_answer(
    db: Db, tenant_id: uuid.UUID, question: str, answer: str, *, expires_in_days: int = 30
) -> uuid.UUID:
    async def create() -> uuid.UUID:
        async with db.sessions() as session:
            row = LibraryAnswer(
                tenant_id=tenant_id,
                question=question,
                answer=answer,
                owner="security@example.com",
                expires_on=date.today() + timedelta(days=expires_in_days),
            )
            session.add(row)
            await session.commit()
            return row.id

    return db.run(create())


def start(
    db: Db, tenant_id: uuid.UUID, questionnaire_id: uuid.UUID, **overrides: float
) -> StartedRun:
    async def go() -> StartedRun:
        async with db.sessions() as session:
            started = await start_run(
                session,
                tenant_id=tenant_id,
                questionnaire_id=questionnaire_id,
                max_cost_usd=overrides.get("max_cost_usd", 5.0),
                enqueue_grace_seconds=overrides.get("enqueue_grace_seconds", 60.0),
            )
            assert started is not None
            return started

    return db.run(go())


def items(db: Db, run_id: uuid.UUID) -> list[RunQuestion]:
    async def load() -> list[RunQuestion]:
        async with db.sessions() as session:
            rows = await session.execute(
                select(RunQuestion)
                .where(RunQuestion.run_id == run_id)
                .order_by(RunQuestion.position)
            )
            return list(rows.scalars())

    return db.run(load())


def sql(db: Db, statement: str, **values: object) -> None:
    async def go() -> None:
        async with db.sessions() as session:
            await session.execute(text(statement), values)
            await session.commit()

    db.run(go())


def expire_lease(db: Db, item_id: uuid.UUID) -> None:
    """As if the worker holding this question had died some time ago."""
    sql(
        db,
        "UPDATE run_questions SET lease_until = now() - interval '1 second' WHERE id = :id",
        id=item_id,
    )


def finished_days_ago(db: Db, item_id: uuid.UUID, days: int) -> None:
    sql(
        db,
        "UPDATE run_questions SET finished_at = now() - make_interval(days => :days) "
        "WHERE id = :id",
        id=item_id,
        days=days,
    )
