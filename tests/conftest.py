from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from standingproof.app import create_app
from standingproof.config import Settings


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A real Postgres with pgvector, started once for the whole test run."""
    with PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture
def client(postgres_url: str, redis_url: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(postgres_url),
        redis_url=SecretStr(redis_url),
        environment="test",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
