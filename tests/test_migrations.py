import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from standingproof.config import get_settings
from standingproof.db import build_engine


async def _fetch_column(database_url: str, sql: str) -> list[str]:
    engine = build_engine(database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text(sql))
            return [row[0] for row in rows]
    finally:
        await engine.dispose()


# A plain `def`, not `async def`: Alembic's env.py calls asyncio.run itself, which cannot
# run inside an event loop that is already running.
def test_upgrade_head_creates_vector_extension_and_tenants(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SP_DATABASE_URL", postgres_url)
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    extensions = asyncio.run(_fetch_column(postgres_url, "SELECT extname FROM pg_extension"))
    tables = asyncio.run(
        _fetch_column(
            postgres_url,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    )

    assert "vector" in extensions
    assert {"tenants", "questionnaires", "questionnaire_files", "questions"} <= set(tables)


def test_orm_models_match_the_migrations(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SP_DATABASE_URL", migrated_postgres_url)
    get_settings.cache_clear()
    try:
        # Raises if autogenerate would produce a new migration, i.e. models and schema differ.
        command.check(Config("alembic.ini"))
    finally:
        get_settings.cache_clear()
