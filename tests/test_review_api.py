"""The review routes and the live connection, through the API."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from starlette.websockets import WebSocketDisconnect

from standingproof.api.events import NOT_FOUND, ORIGIN_REFUSED, origin_allowed
from standingproof.config import Settings
from tests.conftest import ClientFactory, Db
from tests.test_runs_api import start, uploaded
from tests.workflow.test_review import drafted

QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Are backups tested?"]


def library_answer(api: TestClient, question: str, answer: str) -> str:
    response = api.post(
        "/library/answers",
        json={
            "question": question,
            "answer": answer,
            "owner": "security",
            "expires_on": "2099-01-01",
        },
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def run_with_drafts(api: TestClient, db: Db, **first: Any) -> tuple[str, list[dict[str, Any]]]:
    """A run whose questions a worker has drafted. Returns the run id and its questions."""
    run_id = start(api, uploaded(api, QUESTIONS)).json()["id"]
    listed = api.get(f"/runs/{run_id}/questions").json()["items"]
    drafted(db, uuid.UUID(listed[0]["id"]), **first)
    drafted(db, uuid.UUID(listed[1]["id"]), answer="Yes, AES-256.")
    drafted(db, uuid.UUID(listed[2]["id"]), status="needs_review")
    return run_id, api.get(f"/runs/{run_id}/questions").json()["items"]


def decide(api: TestClient, item: dict[str, Any], decision: str, **extra: Any) -> Any:
    body = {"decision": decision, "version": item["version"], **extra}
    return api.post(f"/run-questions/{item['id']}/decision", json=body)


def test_a_question_comes_with_the_text_of_its_sources(api: TestClient, db: Db) -> None:
    source = library_answer(api, "Is MFA enforced?", "Yes, for all staff.")
    _, [first, *_] = run_with_drafts(
        api, db, citations=[f"answer:{source}", "answer:" + str(uuid.uuid4())]
    )

    body = api.get(f"/run-questions/{first['id']}").json()

    assert body["question"] == "Is MFA enforced?"
    assert (body["review"], body["version"], body["final_answer"]) == ("pending", 1, None)
    found, missing = body["sources"]
    assert found["available"] is True
    assert (found["kind"], found["text"], found["owner"]) == (
        "answer",
        "Yes, for all staff.",
        "security",
    )
    assert found["valid_until"] == "2099-01-01"
    assert missing == {
        "id": missing["id"],
        "available": False,
        "kind": None,
        "text": None,
        "label": None,
        "owner": None,
        "valid_until": None,
        "observed_at": None,
    }


def test_the_run_listing_shows_each_questions_review_state(api: TestClient, db: Db) -> None:
    _, listed = run_with_drafts(api, db)

    assert [(q["status"], q["review"]) for q in listed] == [
        ("ready", "pending"),
        ("ready", "pending"),
        ("needs_review", "pending"),
    ]


def test_approve_edit_and_reject(api: TestClient, db: Db) -> None:
    _, [first, second, third] = run_with_drafts(api, db)

    approved = decide(api, first, "approve")
    edited = decide(api, second, "edit", answer="Yes, AES-256, and we are certified.")
    rejected = decide(api, third, "reject", comment="For legal.")

    assert approved.status_code == edited.status_code == rejected.status_code == 200
    assert (approved.json()["review"], approved.json()["final_answer"]) == ("approved", "Yes.")
    assert edited.json()["review"] == "edited"
    assert edited.json()["warnings"] == ["certified"]
    assert edited.json()["answer"] == "Yes, AES-256."
    assert rejected.json()["final_answer"] is None


def test_a_stale_or_repeated_decision_is_409(api: TestClient, db: Db) -> None:
    _, [first, *_] = run_with_drafts(api, db)

    assert decide(api, {**first, "version": 0}, "approve").status_code == 409
    assert decide(api, first, "approve").status_code == 200
    assert decide(api, {**first, "version": 2}, "reject").status_code == 409


def test_a_decision_missing_what_it_needs_is_422(api: TestClient, db: Db) -> None:
    _, [first, _, third] = run_with_drafts(api, db)

    assert decide(api, first, "edit", answer=" ").status_code == 422
    assert decide(api, first, "redraft").status_code == 422
    assert decide(api, third, "approve").status_code == 422
    assert decide(api, first, "delete").status_code == 422
    assert decide(api, first, "approve", surprise=1).status_code == 422


def test_a_redraft_is_queued_for_a_worker(make_api: ClientFactory, db: Db, redis_url: str) -> None:
    stream = f"test:jobs:{uuid.uuid4()}"
    api = make_api(queue_stream=stream)
    run_id, [first, *_] = run_with_drafts(api, db)
    client = Redis.from_url(redis_url)
    before = client.xlen(stream)

    response = decide(api, first, "redraft", comment="Mention contractors.")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["review"]) == ("queued", "redrafting")
    newest = client.xrevrange(stream, count=1)[0][1]
    assert client.xlen(stream) == before + 1
    assert newest == {b"item": first["id"].encode()}
    assert api.get(f"/runs/{run_id}").json()["status"] == "running"
    client.close()


def test_approve_ready_needs_the_count_the_reviewer_saw(api: TestClient, db: Db) -> None:
    run_id, _ = run_with_drafts(api, db)

    wrong = api.post(f"/runs/{run_id}/approve-ready", json={"count": 3})
    right = api.post(f"/runs/{run_id}/approve-ready", json={"count": 2})

    assert wrong.status_code == 409
    assert (right.status_code, right.json()) == (200, {"approved": 2})
    reviews = [q["review"] for q in api.get(f"/runs/{run_id}/questions").json()["items"]]
    assert reviews == ["approved", "approved", "pending"]
    assert api.post(f"/runs/{uuid.uuid4()}/approve-ready", json={"count": 1}).status_code == 404


def test_runs_are_listed_newest_first_with_their_file(api: TestClient) -> None:
    first = start(api, uploaded(api, QUESTIONS)).json()["id"]
    second = start(api, uploaded(api, QUESTIONS[:2])).json()["id"]

    listed = api.get("/runs").json()["items"]

    assert [run["id"] for run in listed] == [second, first]
    assert listed[0]["filename"] == "acme.xlsx"


@pytest.mark.parametrize("bad", ["nonsense", "answer:not-a-uuid", "secret:" + str(uuid.uuid4())])
def test_a_source_that_does_not_resolve_is_404(api: TestClient, bad: str) -> None:
    assert api.get(f"/sources/{bad}").status_code == 404
    assert api.get(f"/sources/answer:{uuid.uuid4()}").status_code == 404


def test_a_source_resolves_for_its_owner_only(make_api: ClientFactory) -> None:
    api = make_api()
    source = library_answer(api, "Is MFA enforced?", "Yes, for all staff.")
    other = make_api(dev_tenant_name="someone-else")

    assert api.get(f"/sources/answer:{source}").json()["text"] == "Yes, for all staff."
    assert other.get(f"/sources/answer:{source}").status_code == 404


def test_another_tenant_cannot_read_or_decide_a_question(make_api: ClientFactory, db: Db) -> None:
    api = make_api()
    _, [first, *_] = run_with_drafts(api, db)
    other = make_api(dev_tenant_name="someone-else")

    assert other.get(f"/run-questions/{first['id']}").status_code == 404
    assert decide(other, first, "approve").status_code == 404
    assert other.get("/runs").json()["items"] == []
    assert api.get(f"/run-questions/{first['id']}").json()["review"] == "pending"


# --- The live connection. ---

ALLOWED = "http://localhost:3000"


def test_the_origin_rule() -> None:
    settings = Settings(
        _env_file=None, allowed_origins="https://app.example.com, http://localhost:3000/"
    )

    assert origin_allowed("https://app.example.com", settings)
    assert origin_allowed("http://localhost:3000", settings)
    assert origin_allowed(None, settings)  # not a browser
    assert not origin_allowed("https://evil.example", settings)
    assert not origin_allowed("https://app.example.com.evil.example", settings)
    assert not origin_allowed("", settings)
    assert not origin_allowed("null", settings)
    assert not origin_allowed("http://localhost:3000", Settings(_env_file=None, allowed_origins=""))


def test_a_watcher_is_told_when_a_decision_is_made(api: TestClient, db: Db) -> None:
    run_id, [first, *_] = run_with_drafts(api, db)

    with api.websocket_connect(f"/runs/{run_id}/events", headers={"origin": ALLOWED}) as socket:
        assert socket.receive_json() == {"type": "ready"}
        decide(api, first, "approve")
        assert socket.receive_json() == {"type": "changed"}


def test_a_watcher_is_told_nothing_but_that_something_changed(api: TestClient, db: Db) -> None:
    run_id, [first, *_] = run_with_drafts(api, db)

    with api.websocket_connect(f"/runs/{run_id}/events", headers={"origin": ALLOWED}) as socket:
        socket.receive_json()
        decide(api, first, "edit", answer="A private answer.")
        message = socket.receive_json()

    assert message == {"type": "changed"}


def test_a_page_from_another_site_cannot_connect(api: TestClient, db: Db) -> None:
    run_id, _ = run_with_drafts(api, db)

    with pytest.raises(WebSocketDisconnect) as refused:
        with api.websocket_connect(
            f"/runs/{run_id}/events", headers={"origin": "https://evil.example"}
        ):
            pass

    assert refused.value.code == ORIGIN_REFUSED


def test_another_tenant_cannot_watch_a_run(make_api: ClientFactory, db: Db) -> None:
    api = make_api()
    run_id, _ = run_with_drafts(api, db)
    other = make_api(dev_tenant_name="someone-else")

    for client, target in ((other, run_id), (api, str(uuid.uuid4()))):
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(f"/runs/{target}/events", headers={"origin": ALLOWED}):
                pass
        assert refused.value.code == NOT_FOUND


# --- What the independent checker found comes with the question. ---

FINDING = {
    "kind": "unsupported",
    "quote": "and contractors",
    "located": True,
    "source_id": None,
    "explanation": "The source covers staff only.",
}


def add_check(db: Db, item_id: str, answer: str, outcome: str = "disagrees") -> None:
    from sqlalchemy import select

    from standingproof.models import DraftCheck, RunQuestion

    async def go() -> None:
        async with db.sessions() as session:
            tenant_id = (
                await session.execute(
                    select(RunQuestion.tenant_id).where(RunQuestion.id == uuid.UUID(item_id))
                )
            ).scalar_one()
            session.add(
                DraftCheck(
                    tenant_id=tenant_id,
                    run_question_id=uuid.UUID(item_id),
                    answer_checked=answer,
                    outcome=outcome,
                    findings=[FINDING] if outcome == "disagrees" else [],
                    model="claude-sonnet-5-5",
                )
            )
            await session.commit()

    db.run(go())


def test_a_question_nobody_checked_has_no_check(api: TestClient, db: Db) -> None:
    _, [first, *_] = run_with_drafts(api, db)

    assert api.get(f"/run-questions/{first['id']}").json()["check"] is None


def test_a_question_comes_with_its_latest_check(api: TestClient, db: Db) -> None:
    wording = "Yes, for all staff and contractors."
    _, [first, *_] = run_with_drafts(api, db, answer=wording)
    add_check(db, first["id"], "An earlier wording.", outcome="agrees")
    add_check(db, first["id"], wording)

    check = api.get(f"/run-questions/{first['id']}").json()["check"]

    assert check == {
        "outcome": "disagrees",
        "findings": [FINDING],
        "model": "claude-sonnet-5-5",
        "on_current_answer": True,
    }


def test_a_check_of_wording_that_was_then_edited_says_so(api: TestClient, db: Db) -> None:
    wording = "Yes, for all staff and contractors."
    _, [first, *_] = run_with_drafts(api, db, answer=wording)
    add_check(db, first["id"], wording)

    edited = decide(api, first, "edit", answer="Yes, for all staff.").json()

    assert edited["final_answer"] == "Yes, for all staff."
    assert edited["check"]["on_current_answer"] is False


def test_another_tenant_cannot_read_a_check(make_api: ClientFactory, db: Db) -> None:
    api = make_api()
    _, [first, *_] = run_with_drafts(api, db)
    add_check(db, first["id"], "Yes.")

    other = make_api(dev_tenant_name="someone-else")

    assert other.get(f"/run-questions/{first['id']}").status_code == 404
