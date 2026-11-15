"""Starting and reading runs through the API. No worker runs here: rows and jobs only."""

import uuid

import httpx2
from fastapi.testclient import TestClient
from redis import Redis

from tests.conftest import ClientFactory
from tests.test_questionnaires_api import scalar, upload
from tests.xlsx_builder import simple

QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Are backups tested?"]


def uploaded(api: TestClient, questions: list[str] = QUESTIONS) -> str:
    response = upload(api, simple(questions))
    assert response.status_code == 201
    return str(response.json()["id"])


def start(api: TestClient, questionnaire_id: str) -> httpx2.Response:
    return api.post(f"/questionnaires/{questionnaire_id}/runs")


def jobs_on(redis_url: str, stream: str) -> list[str]:
    client = Redis.from_url(redis_url)
    try:
        return [fields[b"item"].decode() for _, fields in client.xrange(stream)]
    finally:
        client.close()


def test_starting_a_run_is_accepted_and_queues_one_job_per_question(
    make_api: ClientFactory, redis_url: str
) -> None:
    stream = f"test:jobs:{uuid.uuid4()}"
    api = make_api(queue_stream=stream)

    response = start(api, uploaded(api))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["counts"] == {"queued": 3}
    assert float(body["cost_usd"]) == 0
    assert body["max_cost_usd"] == "5.0000"
    listed = api.get(f"/runs/{body['id']}/questions").json()["items"]
    assert [item["question"] for item in listed] == QUESTIONS
    assert sorted(jobs_on(redis_url, stream)) == sorted(item["id"] for item in listed)


def test_a_job_carries_only_an_id(make_api: ClientFactory, redis_url: str) -> None:
    stream = f"test:jobs:{uuid.uuid4()}"
    api = make_api(queue_stream=stream)
    start(api, uploaded(api))

    client = Redis.from_url(redis_url)
    try:
        entries = client.xrange(stream)
    finally:
        client.close()

    for _, fields in entries:
        assert list(fields) == [b"item"]
        uuid.UUID(fields[b"item"].decode())


def test_starting_again_returns_the_active_run_with_200_and_queues_nothing_more(
    make_api: ClientFactory, redis_url: str
) -> None:
    stream = f"test:jobs:{uuid.uuid4()}"
    api = make_api(queue_stream=stream)
    questionnaire_id = uploaded(api)
    first = start(api, questionnaire_id)

    second = start(api, questionnaire_id)

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(jobs_on(redis_url, stream)) == 3


def test_a_run_is_accepted_when_the_queue_is_unreachable(make_api: ClientFactory) -> None:
    from pydantic import SecretStr

    api = make_api(redis_url=SecretStr("redis://127.0.0.1:1/0"))

    response = start(api, uploaded(api))

    assert response.status_code == 202
    assert response.json()["counts"] == {"queued": 3}


def test_unknown_questionnaire_is_404(api: TestClient) -> None:
    assert start(api, str(uuid.uuid4())).status_code == 404


def test_a_questionnaire_that_needs_its_columns_chosen_is_409(api: TestClient) -> None:
    from tests.xlsx_builder import xlsx

    response = upload(api, xlsx({"Sheet1": [["a", "b"], ["c", "d"]]}))
    assert response.json()["status"] == "needs_mapping"

    assert start(api, response.json()["id"]).status_code == 409


def test_questions_can_be_filtered_and_paged(api: TestClient) -> None:
    run_id = start(api, uploaded(api)).json()["id"]

    first = api.get(f"/runs/{run_id}/questions", params={"limit": 2}).json()
    rest = api.get(
        f"/runs/{run_id}/questions", params={"limit": 2, "after": first["next_after"]}
    ).json()
    none_ready = api.get(f"/runs/{run_id}/questions", params={"status": "ready"}).json()

    assert [item["position"] for item in first["items"]] == [1, 2]
    assert first["next_after"] == 2
    assert [item["position"] for item in rest["items"]] == [3]
    assert rest["next_after"] is None
    assert none_ready["items"] == []
    assert api.get(f"/runs/{run_id}/questions", params={"status": "bogus"}).status_code == 422


def test_cancelling_closes_the_queued_questions(api: TestClient) -> None:
    run_id = start(api, uploaded(api)).json()["id"]

    response = api.post(f"/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["counts"] == {"not_started": 3}
    reasons = {item["reason"] for item in api.get(f"/runs/{run_id}/questions").json()["items"]}
    assert reasons == {"cancelled"}


def test_a_new_run_can_start_after_the_last_one_ended(api: TestClient) -> None:
    questionnaire_id = uploaded(api)
    first = start(api, questionnaire_id).json()["id"]
    api.post(f"/runs/{first}/cancel")

    second = start(api, questionnaire_id)

    assert second.status_code == 202
    assert second.json()["id"] != first


def test_another_tenants_run_is_404_everywhere(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    api = make_api()
    run_id = start(api, uploaded(api)).json()["id"]
    other = make_api(dev_tenant_name="someone-else")

    assert other.get(f"/runs/{run_id}").status_code == 404
    assert other.get(f"/runs/{run_id}/questions").status_code == 404
    assert other.post(f"/runs/{run_id}/cancel").status_code == 404
    assert api.get(f"/runs/{run_id}").json()["status"] == "running"
    assert scalar(migrated_postgres_url, "SELECT count(*) FROM runs") == 1


def test_unknown_run_is_404(api: TestClient) -> None:
    assert api.get(f"/runs/{uuid.uuid4()}").status_code == 404
