import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from alembic.config import Config
from botocore.exceptions import NoCredentialsError
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from alembic import command
from standingproof.agent.tools import ToolRegistry
from standingproof.app import create_app
from standingproof.checks.aws import AwsReader
from standingproof.checks.service import CheckBackends
from standingproof.config import Settings, get_settings
from standingproof.db import build_engine, build_session_factory
from standingproof.library.embedding import Embedder
from standingproof.library.tools import build_library_registry
from standingproof.models import Tenant
from tests.fakes import FakeEmbedder

ClientFactory = Callable[..., TestClient]


def _no_aws_credentials(service: str) -> Any:
    raise NoCredentialsError()


def no_backends() -> CheckBackends:
    """Backends with no credential at all: GitHub absent, AWS unable to build a client."""
    return CheckBackends(
        github=None, aws=AwsReader(region="eu-west-1", client_factory=_no_aws_credentials)
    )


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A real Postgres with pgvector, started once for the whole test run."""
    with PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def migrated_postgres_url(postgres_url: str) -> str:
    """The same database with every migration applied, once per test run."""
    previous = os.environ.get("SP_DATABASE_URL")
    os.environ["SP_DATABASE_URL"] = postgres_url
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            del os.environ["SP_DATABASE_URL"]
        else:
            os.environ["SP_DATABASE_URL"] = previous
        get_settings.cache_clear()
    return postgres_url


async def _execute(database_url: str, sql: str) -> None:
    engine = build_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


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


@pytest.fixture
def make_api(migrated_postgres_url: str, redis_url: str) -> Iterator[ClientFactory]:
    """Build clients against an empty, migrated database. Keyword arguments override settings."""
    # A plain `def` fixture: asyncio.run needs to be outside any running event loop.
    asyncio.run(_execute(migrated_postgres_url, "TRUNCATE tenants CASCADE"))
    with ExitStack() as stack:

        def build(**overrides: object) -> TestClient:
            # No test loads the real embedding model unless it passes its own embedder.
            embedder = overrides.pop("embedder", None) or FakeEmbedder()
            # Nor does any test reach GitHub or AWS, or pick up a developer's own keys.
            backends = overrides.pop("backends", None) or no_backends()
            values: dict[str, object] = {"environment": "test", **overrides}
            settings = Settings(
                _env_file=None,
                database_url=SecretStr(migrated_postgres_url),
                redis_url=SecretStr(redis_url),
                **values,  # type: ignore[arg-type]
            )
            return stack.enter_context(
                TestClient(
                    create_app(settings, embedder=embedder, backends=backends)  # type: ignore[arg-type]
                )
            )

        yield build


@pytest.fixture
def api(make_api: ClientFactory) -> TestClient:
    return make_api()


class Db:
    """One event loop and one connection pool for a test, both closed afterwards."""

    def __init__(self, database_url: str) -> None:
        # One loop for the whole test: a pool belongs to the loop it was first used on.
        self.loop = asyncio.new_event_loop()
        self._engine = build_engine(database_url)
        self.sessions: async_sessionmaker[AsyncSession] = build_session_factory(self._engine)

    def run[T](self, awaitable: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(awaitable)

    def tenant(self, name: str = "default") -> uuid.UUID:
        async def lookup() -> uuid.UUID:
            async with self.sessions() as session:
                statement = select(Tenant.id).where(Tenant.name == name)
                return (await session.execute(statement)).scalar_one()

        return self.run(lookup())

    def registry(self, name: str = "default", embedder: Embedder | None = None) -> ToolRegistry:
        return build_library_registry(self.sessions, self.tenant(name), embedder or FakeEmbedder())

    def close(self) -> None:
        self.run(self._engine.dispose())
        self.loop.close()


@pytest.fixture
def db(migrated_postgres_url: str) -> Iterator[Db]:
    database = Db(migrated_postgres_url)
    yield database
    database.close()
