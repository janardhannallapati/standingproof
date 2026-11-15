"""Storing approved answers and policy documents through the API."""

import asyncio
import datetime as dt
import uuid

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import text

from standingproof.db import build_engine
from tests.conftest import ClientFactory
from tests.fakes import FakeEmbedder

TOMORROW = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)).isoformat()
YESTERDAY = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)).isoformat()

MFA = {
    "question": "Is multi-factor authentication required for employees?",
    "answer": "Yes. Multi-factor authentication is required for all employee accounts.",
    "owner": "security-lead@example.com",
    "expires_on": TOMORROW,
    "control_id": "CC6.1",
}

POLICY = b"""# Access Control Policy

## 4.2 Authentication

All workforce members must authenticate with a second factor.

## 4.3 Reviews

Access is reviewed every quarter.
"""


def add_answer(client: TestClient, **changes: object) -> httpx2.Response:
    return client.post("/library/answers", json={**MFA, **changes})


def upload_policy(
    client: TestClient, content: bytes = POLICY, name: str = "access-control.md"
) -> httpx2.Response:
    return client.post("/library/policies", files={"file": (name, content, "text/markdown")})


def count(database_url: str, table_and_filter: str) -> int:
    async def run() -> int:
        engine = build_engine(database_url)
        try:
            async with engine.connect() as conn:
                statement = text(f"SELECT count(*) FROM {table_and_filter}")  # noqa: S608
                return int((await conn.execute(statement)).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_an_approved_answer_is_stored_and_read_back(api: TestClient) -> None:
    created = add_answer(api)

    assert created.status_code == 201
    body = created.json()
    assert body["question"] == MFA["question"]
    assert body["owner"] == "security-lead@example.com"
    assert body["expires_on"] == TOMORROW
    assert body["control_id"] == "CC6.1"
    assert body["expired"] is False
    assert api.get(f"/library/answers/{body['id']}").json() == body


def test_the_vector_is_stored_with_the_answer_under_the_active_model(
    api: TestClient, migrated_postgres_url: str
) -> None:
    add_answer(api)

    assert count(migrated_postgres_url, "library_answers") == 1
    assert (
        count(
            migrated_postgres_url,
            "library_answer_embeddings WHERE model = 'BAAI/bge-small-en-v1.5' "
            "AND vector_dims(embedding) = 384",
        )
        == 1
    )


def test_if_embedding_fails_nothing_is_stored(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    class BrokenEmbedder(FakeEmbedder):
        async def embed_documents(self, texts):  # type: ignore[no-untyped-def]
            from standingproof.library.embedding import EmbeddingError

            raise EmbeddingError("the provider is down")

    api = make_api(embedder=BrokenEmbedder())

    response = add_answer(api)

    assert response.status_code == 503
    assert count(migrated_postgres_url, "library_answers") == 0


def test_an_answer_needs_its_fields_and_stays_within_the_text_limit(
    make_api: ClientFactory,
) -> None:
    api = make_api(library_text_max_chars=50)

    assert add_answer(api, question="   ").status_code == 422
    assert add_answer(api, owner="").status_code == 422
    assert add_answer(api, expires_on="not a date").status_code == 422
    assert add_answer(api, surprise="field").status_code == 422
    too_long = add_answer(api, answer="x" * 51)
    assert too_long.status_code == 422
    assert "50" in too_long.json()["detail"]


def test_answers_are_listed_in_pages_and_expired_ones_are_marked(api: TestClient) -> None:
    ids = {add_answer(api, question=f"Question {n}?").json()["id"] for n in range(3)}
    ids.add(add_answer(api, question="Old one?", expires_on=YESTERDAY).json()["id"])

    first = api.get("/library/answers", params={"limit": 3}).json()
    second = api.get("/library/answers", params={"limit": 3, "after": first["next_after"]}).json()

    assert len(first["items"]) == 3
    assert len(second["items"]) == 1
    assert second["next_after"] is None
    listed = first["items"] + second["items"]
    assert {item["id"] for item in listed} == ids
    assert [item["expired"] for item in listed if item["question"] == "Old one?"] == [True]


def test_another_tenants_answer_is_not_found(make_api: ClientFactory) -> None:
    ours = make_api()
    theirs = make_api(dev_tenant_name="another-company")
    answer_id = add_answer(ours).json()["id"]

    assert ours.get(f"/library/answers/{answer_id}").status_code == 200
    assert theirs.get(f"/library/answers/{answer_id}").status_code == 404
    assert theirs.get(f"/library/answers/{uuid.uuid4()}").status_code == 404
    assert theirs.get("/library/answers").json()["items"] == []


def test_a_policy_is_split_into_chunks_each_with_a_vector(
    api: TestClient, migrated_postgres_url: str
) -> None:
    response = upload_policy(api)

    assert response.status_code == 201
    assert response.json()["title"] == "Access Control Policy"
    assert response.json()["chunk_count"] == 2
    assert count(migrated_postgres_url, "policy_chunks") == 2
    assert count(migrated_postgres_url, "policy_chunk_embeddings") == 2
    assert (
        count(
            migrated_postgres_url,
            "policy_chunks WHERE heading_path = 'Access Control Policy > 4.2 Authentication'",
        )
        == 1
    )


def test_the_same_policy_uploaded_twice_is_one_policy(
    api: TestClient, migrated_postgres_url: str
) -> None:
    first = upload_policy(api)
    second = upload_policy(api, name="renamed.md")

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert count(migrated_postgres_url, "policy_docs") == 1
    assert count(migrated_postgres_url, "policy_chunks") == 2


def test_a_policy_without_a_top_heading_takes_its_title_from_the_file_name(
    api: TestClient,
) -> None:
    response = upload_policy(api, b"Backups run nightly.\n", name="backup policy.txt")

    assert response.status_code == 201
    assert response.json()["title"] == "backup policy"


def test_policy_uploads_that_are_refused(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    api = make_api(policy_max_bytes=200, policy_max_chunks=2)

    too_large = upload_policy(api, b"# T\n\n" + b"word " * 100)
    wrong_type = upload_policy(api, POLICY[:150], name="policy.pdf")
    binary = upload_policy(api, b"\x00\x01\x02\xff\xfe", name="policy.txt")
    empty = upload_policy(api, b"   \n", name="empty.md")
    too_many = upload_policy(api, b"# A\n\na\n\n# B\n\nb\n\n# C\n\nc\n")

    assert too_large.status_code == 413
    assert wrong_type.status_code == 415
    assert binary.status_code == 415
    assert empty.status_code == 422
    assert too_many.status_code == 422
    assert "2" in too_many.json()["detail"]
    assert count(migrated_postgres_url, "policy_docs") == 0
