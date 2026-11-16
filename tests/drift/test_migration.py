"""Migration 0010: the drift tables, and what existing answers rest on, filled in."""

import asyncio
import json
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from standingproof.config import get_settings
from standingproof.db import build_engine

FACTS = json.dumps({"protected": True})


async def _run(database_url: str, statements: list[tuple[str, dict[str, object]]]) -> None:
    engine = build_engine(database_url)
    try:
        async with engine.begin() as conn:
            for sql, values in statements:
                await conn.execute(text(sql), values)
    finally:
        await engine.dispose()


async def _rows(database_url: str, sql: str) -> list[tuple[object, ...]]:
    engine = build_engine(database_url)
    try:
        async with engine.connect() as conn:
            return [tuple(row) for row in await conn.execute(text(sql))]
    finally:
        await engine.dispose()


def _alembic(database_url: str, monkeypatch: pytest.MonkeyPatch, direction: str, to: str) -> None:
    monkeypatch.setenv("SP_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        getattr(command, direction)(Config("alembic.ini"), to)
    finally:
        get_settings.cache_clear()


def test_the_drift_tables_exist(migrated_postgres_url: str) -> None:
    tables = {
        row[0]
        for row in asyncio.run(
            _rows(
                migrated_postgres_url,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
            )
        )
    }
    columns = {
        row[0]
        for row in asyncio.run(
            _rows(
                migrated_postgres_url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'run_questions'",
            )
        )
    }

    assert {
        "answer_evidence",
        "drift_queue",
        "drift_events",
        "answer_reopenings",
        "webhook_deliveries",
    } <= tables
    assert "reopened_at" in columns


def test_answers_that_already_cite_a_live_check_are_linked_to_it(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = migrated_postgres_url
    asyncio.run(_run(url, [("TRUNCATE tenants CASCADE", {})]))
    _alembic(url, monkeypatch, "downgrade", "0009")
    ids = {name: uuid.uuid4() for name in ["t", "qn", "q1", "q2", "q3", "run", "seen", "failed"]}
    citing = uuid.uuid4()
    cites = json.dumps(
        [f"evidence:{ids['seen']}", f"evidence:{ids['failed']}", f"answer:{uuid.uuid4()}", "junk"]
    )
    evidence = (
        "INSERT INTO evidence (id, tenant_id, check_name, scope, status, facts, raw_result, "
        "statement, observed_at) VALUES (:id, :t, 'github.branch_protection', 'acme/app', "
        ":status, CAST(:facts AS jsonb), '{}', 's', now())"
    )
    item = (
        "INSERT INTO run_questions (id, tenant_id, run_id, question_id, position, status, "
        "citations) VALUES (:id, :t, :run, :q, :position, 'ready', CAST(:cites AS jsonb))"
    )
    try:
        asyncio.run(
            _run(
                url,
                [
                    ("INSERT INTO tenants (id, name) VALUES (:t, 'backfill')", ids),
                    (
                        "INSERT INTO questionnaires (id, tenant_id, filename, status, sha256) "
                        "VALUES (:qn, :t, 'q.xlsx', 'parsed', 'x')",
                        ids,
                    ),
                    *[
                        (
                            "INSERT INTO questions (id, tenant_id, questionnaire_id, position, "
                            "sheet, cell, text) VALUES (:q, :t, :qn, :position, 'S', 'A1', 'q')",
                            {**ids, "q": ids[name], "position": position},
                        )
                        for position, name in enumerate(["q1", "q2", "q3"], start=1)
                    ],
                    (
                        "INSERT INTO runs (id, tenant_id, questionnaire_id, status, max_cost_usd) "
                        "VALUES (:run, :t, :qn, 'finished', 5)",
                        ids,
                    ),
                    (evidence, {**ids, "id": ids["seen"], "status": "observed", "facts": FACTS}),
                    (
                        evidence,
                        {**ids, "id": ids["failed"], "status": "unavailable", "facts": "{}"},
                    ),
                    (item, {**ids, "id": citing, "q": ids["q1"], "position": 1, "cites": cites}),
                    # No citations, and citations that are not a list: neither breaks it.
                    (
                        item,
                        {**ids, "id": uuid.uuid4(), "q": ids["q2"], "position": 2, "cites": None},
                    ),
                    (
                        item,
                        {**ids, "id": uuid.uuid4(), "q": ids["q3"], "position": 3, "cites": "{}"},
                    ),
                ],
            )
        )
    finally:
        _alembic(url, monkeypatch, "upgrade", "head")

    linked = asyncio.run(
        _rows(url, "SELECT run_question_id, check_name, scope, evidence_id FROM answer_evidence")
    )

    assert linked == [(citing, "github.branch_protection", "acme/app", ids["seen"])]
