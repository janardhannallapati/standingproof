from fastapi.testclient import TestClient
from pydantic import SecretStr

from standingproof.app import create_app
from standingproof.config import Settings


def test_healthz_returns_ok_without_any_store() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ok_when_both_stores_answer(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "redis": "ok"},
    }


def test_readyz_503_names_the_store_that_is_down(postgres_url: str) -> None:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(postgres_url),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        environment="test",
    )
    with TestClient(create_app(settings)) as broken:
        response = broken.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": "ok", "redis": "failed"}
