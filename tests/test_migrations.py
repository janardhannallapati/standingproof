import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from standingproof.config import get_settings
from standingproof.db import build_engine
from standingproof.library.embedding import MODELS


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
    assert {
        "library_answers",
        "policy_docs",
        "policy_chunks",
        "library_answer_embeddings",
        "policy_chunk_embeddings",
    } <= set(tables)


def test_every_known_embedding_model_has_its_two_vector_indexes(
    migrated_postgres_url: str,
) -> None:
    indexes = asyncio.run(
        _fetch_column(
            migrated_postgres_url,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'",
        )
    )
    by_name = {definition.split(" ON ")[0].split()[-1]: definition for definition in indexes}

    for spec in MODELS.values():
        for table in ("library_answer_embeddings", "policy_chunk_embeddings"):
            definition = by_name[f"ix_{table}_{spec.index_suffix}"]
            assert "USING hnsw" in definition
            assert f"vector({spec.dimensions})" in definition
            assert "vector_cosine_ops" in definition
            assert spec.name in definition  # the WHERE model = '...' clause


def test_search_text_is_kept_by_the_database_and_indexed(migrated_postgres_url: str) -> None:
    generated = asyncio.run(
        _fetch_column(
            migrated_postgres_url,
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'search_text' AND is_generated = 'ALWAYS'",
        )
    )
    gin = asyncio.run(
        _fetch_column(
            migrated_postgres_url,
            "SELECT tablename FROM pg_indexes WHERE indexdef LIKE '%USING gin (search_text)%'",
        )
    )

    assert set(generated) == {"library_answers", "policy_chunks"}
    assert set(gin) == {"library_answers", "policy_chunks"}


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
