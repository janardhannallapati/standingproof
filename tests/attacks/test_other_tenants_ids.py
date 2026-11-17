"""One tenant asking the API for another tenant's things, by id and by listing.

The routes are read from the application, not listed here, so a new route is attacked the
day it is added. A path parameter this file has not been told about fails the test: its
author must say whether it names something a tenant owns.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from tests.conftest import ClientFactory, Db
from tests.drift.rig import approved_answer

# Path parameters that name something a tenant owns.
OWNED = {
    "questionnaire_id",
    "run_id",
    "item_id",
    "evidence_id",
    "answer_id",
    "file_id",
    "source_id",
}
# Path parameters that do not: the name of a check is the same for every tenant.
SHARED = {"name"}
# What a well-formed request to each writing route looks like, so that the refusal comes
# from "not yours" and not from "not valid".
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", "/questionnaires/{questionnaire_id}/mapping"): {
        "sheet": "S",
        "header_row": 1,
        "question_column": "A",
    },
    ("POST", "/runs/{run_id}/approve-ready"): {"count": 1},
    ("POST", "/run-questions/{item_id}/decision"): {
        "decision": "reject",
        "version": 1,
        "comment": "x",
    },
    ("POST", "/runs/{run_id}/file/{file_id}/sent"): {"customer": "Acme"},
}


def _theirs(db: Db) -> dict[str, str]:
    """A second tenant with one of everything, and the ids a caller would have to guess."""
    owner, item, evidence = approved_answer(db, "theirs")

    async def go() -> dict[str, str]:
        async with db.sessions() as session:
            answer = (
                await session.execute(
                    text(
                        "INSERT INTO library_answers"
                        " (tenant_id, question, answer, owner, expires_on)"
                        " VALUES (:t, 'Their question', 'THEIR-SECRET-ANSWER', 'o', '2099-01-01')"
                        " RETURNING id"
                    ),
                    {"t": owner},
                )
            ).scalar_one()
            run, questionnaire = (
                await session.execute(
                    text("SELECT r.id, r.questionnaire_id FROM runs r WHERE r.tenant_id = :t"),
                    {"t": owner},
                )
            ).one()
            await session.commit()
        return {
            "questionnaire_id": str(questionnaire),
            "run_id": str(run),
            "item_id": str(item),
            "evidence_id": str(evidence),
            "answer_id": str(answer),
            "file_id": str(uuid.uuid4()),
            "source_id": f"evidence:{evidence}",
        }

    return db.run(go())


def _nobodys() -> dict[str, str]:
    ids = {name: str(uuid.uuid4()) for name in OWNED}
    ids["source_id"] = f"evidence:{uuid.uuid4()}"
    return ids


def _snapshot(db: Db) -> list[Any]:
    """Everything of theirs that a request could have changed."""

    async def go() -> list[Any]:
        async with db.sessions() as session:
            found: list[Any] = []
            for table in (
                "runs",
                "run_questions",
                "review_decisions",
                "questionnaires",
                "filled_files",
                "sent_files",
            ):
                # Table names are the constants above.
                digest = "md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))"
                statement = f"SELECT {digest} FROM {table} t"  # noqa: S608
                found.append((table, (await session.execute(text(statement))).scalar_one()))
            return found

    return db.run(go())


def _routes(api: TestClient) -> list[tuple[str, str, list[str]]]:
    found = []
    for path, operations in api.app.openapi()["paths"].items():  # type: ignore[attr-defined]
        names = [part[1:-1] for part in path.split("/") if part.startswith("{")]
        for method in operations:
            found.append((method.upper(), path, names))
    return found


def test_every_route_answers_for_another_tenants_id_as_for_no_id_at_all(
    make_api: ClientFactory, db: Db
) -> None:
    api = make_api(dev_tenant_name="ours")
    api.get("/runs")  # our tenant exists from here on
    theirs, nobodys = _theirs(db), _nobodys()
    before = _snapshot(db)
    attacked = 0

    for method, path, names in _routes(api):
        unknown = set(names) - OWNED - SHARED
        assert not unknown, f"{path}: say in this file whether {unknown} names something owned"
        if not set(names) & OWNED:
            continue
        body = BODIES.get((method, path))
        replies = []
        for ids in (theirs, nobodys):
            url = path.format(**{name: ids.get(name, "github.branch_protection") for name in names})
            reply = api.request(method, url, json=body)
            replies.append((reply.status_code, reply.text.replace(ids[names[0]], "ID")))
        assert replies[0] == replies[1], f"{method} {path} tells theirs from nobody's"
        assert replies[0][0] == 404, f"{method} {path}: {replies[0]}"
        attacked += 1

    assert attacked >= 15
    assert _snapshot(db) == before


def test_no_listing_shows_another_tenants_rows(make_api: ClientFactory, db: Db) -> None:
    api = make_api(dev_tenant_name="ours")
    api.get("/runs")
    theirs = _theirs(db)
    listed = 0

    for method, path, names in _routes(api):
        if method != "GET" or names or path in ("/healthz", "/readyz"):
            continue
        reply = api.get(path, params={"q": "Their question"} if path == "/library/search" else None)
        assert reply.status_code == 200, path
        for value in (*theirs.values(), "THEIR-SECRET-ANSWER", "Their question"):
            assert value not in reply.text, f"{path} shows {value}"
        listed += 1

    assert listed >= 7


def test_the_live_connection_refuses_another_tenants_run(make_api: ClientFactory, db: Db) -> None:
    api = make_api(dev_tenant_name="ours")
    api.get("/runs")
    theirs = _theirs(db)

    for run_id in (theirs["run_id"], str(uuid.uuid4())):
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            api.websocket_connect(f"/runs/{run_id}/events"),
        ):
            pass
        assert closed.value.code == 4404
