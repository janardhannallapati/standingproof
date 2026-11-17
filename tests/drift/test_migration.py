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
from tests.conftest import EMPTY_THE_DATABASE

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
    asyncio.run(_run(url, [(statement, {}) for statement in EMPTY_THE_DATABASE]))
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


def test_existing_observations_keep_their_order_and_new_ones_come_after(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0011: `seq` is filled in the order `observed_at` gave until now."""
    url = migrated_postgres_url
    asyncio.run(_run(url, [(statement, {}) for statement in EMPTY_THE_DATABASE]))
    _alembic(url, monkeypatch, "downgrade", "0010")
    tenant, old, new, later = (uuid.uuid4() for _ in range(4))
    evidence = (
        "INSERT INTO evidence (id, tenant_id, check_name, scope, status, facts, raw_result, "
        "statement, observed_at) VALUES (:id, :t, 'github.branch_protection', 'acme/app', "
        "'observed', '{}', '{}', 's', now() - make_interval(secs => :ago))"
    )
    try:
        asyncio.run(
            _run(
                url,
                [
                    ("INSERT INTO tenants (id, name) VALUES (:t, 'order')", {"t": tenant}),
                    # Stored newest first, so physical order is not the answer.
                    (evidence, {"id": new, "t": tenant, "ago": 10}),
                    (evidence, {"id": old, "t": tenant, "ago": 20}),
                ],
            )
        )
    finally:
        _alembic(url, monkeypatch, "upgrade", "head")
    # After the migration a look with an earlier clock time still sorts last.
    asyncio.run(_run(url, [(evidence, {"id": later, "t": tenant, "ago": 30})]))

    rows = asyncio.run(_rows(url, "SELECT id FROM evidence ORDER BY seq"))
    assert [row[0] for row in rows] == [old, new, later]
    with pytest.raises(Exception, match="seq"):
        asyncio.run(_run(url, [("UPDATE evidence SET seq = 99 WHERE id = :id", {"id": old})]))


def test_existing_decisions_and_reopenings_keep_their_order_and_a_waiting_answer_its_first(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0018: `seq` follows the timestamps used until now, and an answer that is
    waiting is given the number of its first reopening."""
    url = migrated_postgres_url
    asyncio.run(_run(url, [(statement, {}) for statement in EMPTY_THE_DATABASE]))
    _alembic(url, monkeypatch, "downgrade", "0017")
    names = ["t", "qn", "q", "run", "item", "e1", "e2", "e3", "d1", "d2", "d3"]
    ids = {name: uuid.uuid4() for name in names}
    evidence = (
        "INSERT INTO evidence (id, tenant_id, check_name, scope, status, facts, raw_result, "
        "statement, observed_at) VALUES (:id, :t, 'github.branch_protection', 'acme/app', "
        "'observed', '{}', '{}', 's', now())"
    )
    event = (
        "INSERT INTO drift_events (id, tenant_id, check_name, scope, evidence_id, trigger) "
        "VALUES (:id, :t, 'github.branch_protection', 'acme/app', :e, 'schedule')"
    )
    reopening = (
        "INSERT INTO answer_reopenings (tenant_id, drift_event_id, run_question_id, "
        "was_evidence_id, changes, was_review, reopened_at) VALUES (:t, :d, :item, :e, '[]', "
        ":was, now() - make_interval(mins => :ago))"
    )
    decision = (
        "INSERT INTO review_decisions (tenant_id, run_question_id, decision, decided_by, "
        "comment, decided_at) VALUES (:t, :item, 'redraft', 'asha', :c, "
        "now() - make_interval(mins => :ago))"
    )
    try:
        asyncio.run(
            _run(
                url,
                [
                    ("INSERT INTO tenants (id, name) VALUES (:t, 'order')", ids),
                    (
                        "INSERT INTO questionnaires (id, tenant_id, filename, status, sha256) "
                        "VALUES (:qn, :t, 'q.xlsx', 'parsed', 'x')",
                        ids,
                    ),
                    (
                        "INSERT INTO questions (id, tenant_id, questionnaire_id, position, "
                        "sheet, cell, text) VALUES (:q, :t, :qn, 1, 'S', 'A1', 'q')",
                        ids,
                    ),
                    (
                        "INSERT INTO runs (id, tenant_id, questionnaire_id, status, max_cost_usd) "
                        "VALUES (:run, :t, :qn, 'finished', 5)",
                        ids,
                    ),
                    # Reopened 20 minutes ago, and waiting since.
                    (
                        "INSERT INTO run_questions (id, tenant_id, run_id, question_id, position, "
                        "status, review, reopened_at) VALUES (:item, :t, :run, :q, 1, 'ready', "
                        "'pending', now() - make_interval(mins => 20))",
                        ids,
                    ),
                    *[(evidence, {**ids, "id": ids[e]}) for e in ("e1", "e2", "e3")],
                    *[
                        (event, {**ids, "id": ids[d], "e": ids[e]})
                        for d, e in (("d1", "e1"), ("d2", "e2"), ("d3", "e3"))
                    ],
                    # Stored newest first, so physical order is not the answer. The oldest
                    # belongs to an earlier wait that a decision closed.
                    (reopening, {**ids, "d": ids["d3"], "e": ids["e3"], "was": None, "ago": 10}),
                    (
                        reopening,
                        {**ids, "d": ids["d2"], "e": ids["e2"], "was": "edited", "ago": 20},
                    ),
                    (
                        reopening,
                        {**ids, "d": ids["d1"], "e": ids["e1"], "was": "approved", "ago": 90},
                    ),
                    (decision, {**ids, "c": "second", "ago": 30}),
                    (decision, {**ids, "c": "first", "ago": 60}),
                ],
            )
        )
    finally:
        _alembic(url, monkeypatch, "upgrade", "head")
    # After the migration a decision with an earlier clock time still sorts last.
    asyncio.run(_run(url, [(decision, {**ids, "c": "third", "ago": 600})]))

    decisions = asyncio.run(_rows(url, "SELECT comment FROM review_decisions ORDER BY seq"))
    reopenings = asyncio.run(_rows(url, "SELECT was_review FROM answer_reopenings ORDER BY seq"))
    first_of_this_wait = asyncio.run(
        _rows(
            url,
            "SELECT r.was_review FROM run_questions q "
            "JOIN answer_reopenings r ON r.seq = q.reopened_seq",
        )
    )

    assert [row[0] for row in decisions] == ["first", "second", "third"]
    assert [row[0] for row in reopenings] == ["approved", "edited", None]
    assert first_of_this_wait == [("edited",)]
