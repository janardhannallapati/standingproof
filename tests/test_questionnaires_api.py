import asyncio
import hashlib
import time
import uuid
from collections.abc import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from standingproof.api.deps import current_tenant
from standingproof.app import create_app
from standingproof.config import Settings
from standingproof.db import build_engine
from standingproof.questionnaires import service
from tests.conftest import ClientFactory
from tests.xlsx_builder import rewritten, simple, xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Are backups tested?"]


def upload(client: TestClient, content: bytes, name: str = "acme.xlsx") -> httpx2.Response:
    return client.post("/questionnaires", files={"file": (name, content, XLSX)})


async def _scalar(database_url: str, sql: str) -> object:
    engine = build_engine(database_url)
    try:
        async with engine.begin() as conn:  # begin() commits, so an INSERT here persists
            return (await conn.execute(text(sql))).scalar_one()
    finally:
        await engine.dispose()


def scalar(database_url: str, sql: str) -> object:
    return asyncio.run(_scalar(database_url, sql))


def test_upload_returns_201_and_the_questions_become_rows(api: TestClient) -> None:
    response = upload(api, simple(QUESTIONS))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parsed"
    assert body["filename"] == "acme.xlsx"
    assert body["question_count"] == 3
    assert body["sheets"] is None

    listed = api.get(f"/questionnaires/{body['id']}/questions").json()
    assert [(q["sheet"], q["cell"], q["text"]) for q in listed["items"]] == [
        ("Security", "B2", QUESTIONS[0]),
        ("Security", "B3", QUESTIONS[1]),
        ("Security", "B4", QUESTIONS[2]),
    ]
    assert listed["items"][0]["answer_cell"] == "C2"
    assert listed["items"][0]["external_id"] == "Q1"


def test_the_stored_file_is_byte_identical_to_the_upload(
    api: TestClient, migrated_postgres_url: str
) -> None:
    content = simple(QUESTIONS)

    upload(api, content)

    stored = scalar(migrated_postgres_url, "SELECT content FROM questionnaire_files")
    assert isinstance(stored, bytes)
    assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(content).hexdigest()


def test_the_same_file_again_returns_200_and_the_first_questionnaire(
    api: TestClient, migrated_postgres_url: str
) -> None:
    content = simple(QUESTIONS)
    first = upload(api, content)

    second = upload(api, content, name="acme-copy.xlsx")

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert scalar(migrated_postgres_url, "SELECT count(*) FROM questionnaires") == 1
    assert scalar(migrated_postgres_url, "SELECT count(*) FROM questions") == 3


def test_an_oversized_upload_is_413_and_nothing_is_stored(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    small = make_api(upload_max_bytes=2_000)

    response = upload(small, simple(QUESTIONS))

    assert response.status_code == 413
    assert "2000" in response.json()["detail"]
    assert scalar(migrated_postgres_url, "SELECT count(*) FROM questionnaires") == 0


def test_a_body_far_over_the_limit_is_refused_before_it_is_read(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    small = make_api(upload_max_bytes=2_000)

    response = upload(small, b"\x00" * 200_000)

    assert response.status_code == 413
    assert scalar(migrated_postgres_url, "SELECT count(*) FROM questionnaires") == 0


def test_a_streamed_body_with_no_declared_length_is_cut_off_at_the_limit(
    make_api: ClientFactory,
) -> None:
    small = make_api(upload_max_bytes=2_000)
    sent = 0

    def endless() -> Iterator[bytes]:
        nonlocal sent
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.xlsx"\r\n\r\n'
        while sent < 5_000_000:
            sent += 16_384
            yield b"\x00" * 16_384

    response = small.post(
        "/questionnaires",
        content=endless(),
        headers={"content-type": "multipart/form-data; boundary=b"},
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    "content",
    [
        b"ID,Question\n1,Is MFA enforced?\n",
        rewritten(simple(QUESTIONS), {"xl/vbaProject.bin": b"\x00macro"}),
    ],
    ids=["text file renamed", "macro workbook"],
)
def test_a_file_that_is_not_a_plain_xlsx_is_415(api: TestClient, content: bytes) -> None:
    response = upload(api, content)

    assert response.status_code == 415


def test_a_workbook_over_a_parsing_limit_is_422(make_api: ClientFactory) -> None:
    strict = make_api(upload_max_questions=2)

    response = upload(strict, simple(QUESTIONS))

    assert response.status_code == 422
    assert "questions" in response.json()["detail"]


def test_an_upload_with_no_file_is_422(api: TestClient) -> None:
    assert api.post("/questionnaires").status_code == 422


def test_no_recognisable_column_is_stored_as_needs_mapping_with_a_preview(
    api: TestClient,
) -> None:
    content = xlsx({"Sheet A": [["Topic", "Details"], ["Access", "Is MFA enforced?"]]})

    response = upload(api, content)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "needs_mapping"
    assert body["question_count"] == 0
    assert body["sheets"] == [
        {"name": "Sheet A", "rows": [["Topic", "Details"], ["Access", "Is MFA enforced?"]]}
    ]
    # The preview is rebuilt from the stored file on a later read.
    assert api.get(f"/questionnaires/{body['id']}").json()["sheets"] == body["sheets"]


def test_a_mapping_parses_the_stored_file(api: TestClient) -> None:
    content = xlsx({"Sheet A": [["Topic", "Details"], ["Access", "Is MFA enforced?"]]})
    created = upload(api, content).json()

    response = api.put(
        f"/questionnaires/{created['id']}/mapping",
        json={"sheet": "Sheet A", "header_row": 1, "question_column": "B"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "parsed"
    assert response.json()["question_count"] == 1
    items = api.get(f"/questionnaires/{created['id']}/questions").json()["items"]
    assert [(q["cell"], q["text"]) for q in items] == [("B2", "Is MFA enforced?")]


def test_a_mapping_that_does_not_fit_the_file_is_422(api: TestClient) -> None:
    content = xlsx({"Sheet A": [["Topic", "Details"], ["Access", "Is MFA enforced?"]]})
    created = upload(api, content).json()
    url = f"/questionnaires/{created['id']}/mapping"

    missing_sheet = api.put(url, json={"sheet": "Nope", "header_row": 1, "question_column": "B"})
    empty_column = api.put(url, json={"sheet": "Sheet A", "header_row": 1, "question_column": "Z"})
    bad_letters = api.put(url, json={"sheet": "Sheet A", "header_row": 1, "question_column": "7"})

    assert [r.status_code for r in (missing_sheet, empty_column, bad_letters)] == [422, 422, 422]
    assert api.get(f"/questionnaires/{created['id']}").json()["status"] == "needs_mapping"


def test_mapping_an_already_parsed_questionnaire_is_409(api: TestClient) -> None:
    created = upload(api, simple(QUESTIONS)).json()

    response = api.put(
        f"/questionnaires/{created['id']}/mapping",
        json={"sheet": "Security", "header_row": 1, "question_column": "B"},
    )

    assert response.status_code == 409


def test_questions_are_paged_in_order(api: TestClient) -> None:
    content = xlsx(
        {
            "Access": [["Question"], ["a1?"], ["a2?"]],
            "Data": [["Question"], ["d1?"], ["d2?"], ["d3?"]],
        }
    )
    url = f"/questionnaires/{upload(api, content).json()['id']}/questions"

    first = api.get(url, params={"limit": 2}).json()
    second = api.get(url, params={"limit": 2, "after": first["next_after"]}).json()
    third = api.get(url, params={"limit": 2, "after": second["next_after"]}).json()

    assert [q["text"] for q in first["items"]] == ["a1?", "a2?"]
    assert [q["text"] for q in second["items"]] == ["d1?", "d2?"]
    assert [q["text"] for q in third["items"]] == ["d3?"]
    assert third["next_after"] is None


def test_page_size_is_bounded(api: TestClient) -> None:
    url = f"/questionnaires/{upload(api, simple(QUESTIONS)).json()['id']}/questions"

    assert api.get(url, params={"limit": 0}).status_code == 422
    assert api.get(url, params={"limit": 501}).status_code == 422


def test_unknown_and_malformed_ids_are_404_and_422(api: TestClient) -> None:
    assert api.get(f"/questionnaires/{uuid.uuid4()}").status_code == 404
    assert api.get(f"/questionnaires/{uuid.uuid4()}/questions").status_code == 404
    assert api.get("/questionnaires/not-a-uuid").status_code == 422


def test_another_tenants_questionnaire_is_404_on_every_route(
    api: TestClient, migrated_postgres_url: str
) -> None:
    created = upload(api, simple(QUESTIONS)).json()
    other = scalar(
        migrated_postgres_url, "INSERT INTO tenants (name) VALUES ('other') RETURNING id"
    )

    async def as_other_tenant() -> uuid.UUID:
        assert isinstance(other, uuid.UUID)
        return other

    api.app.dependency_overrides[current_tenant] = as_other_tenant  # type: ignore[attr-defined]
    try:
        url = f"/questionnaires/{created['id']}"
        mapping = {"sheet": "Security", "header_row": 1, "question_column": "B"}
        statuses = [
            api.get(url).status_code,
            api.get(f"{url}/questions").status_code,
            api.put(f"{url}/mapping", json=mapping).status_code,
        ]
        # The other tenant uploading the same bytes gets its own questionnaire.
        own = upload(api, simple(QUESTIONS))
    finally:
        api.app.dependency_overrides.clear()  # type: ignore[attr-defined]

    assert statuses == [404, 404, 404]
    assert own.status_code == 201
    assert own.json()["id"] != created["id"]


async def test_parsing_does_not_block_other_requests(
    make_api: ClientFactory,
    migrated_postgres_url: str,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_api()  # empties the database
    real_parse = service.parse_workbook
    moments: dict[str, float] = {}
    loop = asyncio.get_running_loop()
    parse_started = asyncio.Event()

    def slow_parse(*args: object, **kwargs: object) -> object:
        # The parser runs in another thread; this is the thread-safe way to signal the loop.
        loop.call_soon_threadsafe(parse_started.set)
        time.sleep(0.5)  # a blocking call, as CPU-bound parsing would be
        moments["parse_finished"] = time.monotonic()
        return real_parse(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "parse_workbook", slow_parse)
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(migrated_postgres_url),
        redis_url=SecretStr(redis_url),
        environment="test",
    )
    app = create_app(settings)

    async def health_while_parsing(client: httpx2.AsyncClient) -> httpx2.Response:
        # Wait until the parser is running, then ask. If the parser were on the event
        # loop, this coroutine could not resume, let alone be answered, until it finished.
        await parse_started.wait()
        response = await client.get("/healthz")
        moments["health_answered"] = time.monotonic()
        return response

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        files = {"file": ("acme.xlsx", simple(QUESTIONS), XLSX)}
        uploaded, health = await asyncio.gather(
            client.post("/questionnaires", files=files), health_while_parsing(client)
        )

    assert uploaded.status_code == 201
    assert health.status_code == 200
    assert moments["health_answered"] < moments["parse_finished"]


def test_without_sign_in_the_api_refuses_to_serve_tenant_data_in_production(
    make_api: ClientFactory,
) -> None:
    production = make_api(environment="prod")

    assert upload(production, simple(QUESTIONS)).status_code == 401
    assert production.get(f"/questionnaires/{uuid.uuid4()}").status_code == 401
    assert production.get("/healthz").status_code == 200


def test_reading_a_questionnaire_does_not_parse_the_file_again(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = xlsx({"Sheet A": [["Topic", "Details"], ["Access", "Is MFA enforced?"]]})
    created = upload(api, content).json()

    def must_not_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a read must not parse the stored file")

    monkeypatch.setattr(service, "parse_workbook", must_not_parse)

    assert api.get(f"/questionnaires/{created['id']}").json()["sheets"] == created["sheets"]
    assert upload(api, content).json()["sheets"] == created["sheets"]


def test_a_comments_column_is_stored_with_each_question(api: TestClient) -> None:
    from tests.xlsx_builder import xlsx

    rows = [["ID", "Question", "Answer", "Comments"], ["Q1", "Is MFA enforced?", None, None]]
    body = upload(api, xlsx({"Security": rows})).json()

    [question] = api.get(f"/questionnaires/{body['id']}/questions").json()["items"]

    assert (question["answer_cell"], question["comment_cell"]) == ("C2", "D2")
