from fastapi.testclient import TestClient

from standingproof.app import create_app


def test_healthz_returns_ok_without_any_store() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
