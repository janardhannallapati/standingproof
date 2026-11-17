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
        "connections",
        "evidence",
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


def test_draft_checks_exists_with_an_index_for_the_latest_check(migrated_postgres_url: str) -> None:
    columns = asyncio.run(
        _fetch_column(
            migrated_postgres_url,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'draft_checks'",
        )
    )
    indexes = asyncio.run(
        _fetch_column(
            migrated_postgres_url,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'draft_checks'",
        )
    )

    assert set(columns) == {
        "id",
        "tenant_id",
        "run_question_id",
        "answer_checked",
        "outcome",
        "findings",
        "model",
        "cost_usd",
        "checked_at",
    }
    assert "ix_draft_checks_question" in indexes


def test_migrations_run_up_and_down_for_an_owner_who_is_not_a_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its own server: roles belong to a server, and here the owner must be the one who
    # creates them, as on a managed database where nobody is a superuser.
    from sqlalchemy import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from testcontainers.community.postgres import PostgresContainer

    async def as_superuser(url: str, *statements: str) -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                for sql in statements:
                    await conn.execute(text(sql))
        finally:
            await engine.dispose()

    with PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg") as pg:
        superuser = make_url(pg.get_connection_url())
        asyncio.run(
            as_superuser(
                superuser.render_as_string(hide_password=False),
                "CREATE ROLE plain_owner LOGIN CREATEROLE NOSUPERUSER PASSWORD 'made-up'",
                "CREATE DATABASE plain OWNER plain_owner",
            )
        )
        # An extension is the one thing such an owner is given by its provider.
        asyncio.run(
            as_superuser(
                superuser.set(database="plain").render_as_string(hide_password=False),
                "CREATE EXTENSION vector",
            )
        )
        owner = superuser.set(
            username="plain_owner",
            password="made-up",  # noqa: S106 - a throwaway server's made-up login
            database="plain",
        )
        owner_url = owner.render_as_string(hide_password=False)
        monkeypatch.setenv("SP_DATABASE_URL", owner_url)
        get_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
            command.downgrade(Config("alembic.ini"), "0016")
            command.upgrade(Config("alembic.ini"), "head")
            # A role of that name that someone could sign in as, or that is exempt from the
            # policies, must not be handed the functions: the migration stops instead.
            command.downgrade(Config("alembic.ini"), "0016")
            for unsafe, safe in (("LOGIN", "NOLOGIN"), ("BYPASSRLS", "NOBYPASSRLS")):
                admin = superuser.set(database="plain").render_as_string(hide_password=False)
                asyncio.run(as_superuser(admin, f"ALTER ROLE standingproof_deleter {unsafe}"))
                with pytest.raises(Exception, match="standingproof_deleter must be a role"):
                    command.upgrade(Config("alembic.ini"), "head")
                asyncio.run(as_superuser(admin, f"ALTER ROLE standingproof_deleter {safe}"))
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            get_settings.cache_clear()

        owners = asyncio.run(
            _fetch_column(
                owner_url,
                "SELECT pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE proname IN ('forget_answer_evidence', 'drop_from_drift_queue')",
            )
        )
        can_act_as = asyncio.run(
            _fetch_column(
                owner_url,
                "SELECT pg_has_role(current_user, 'standingproof_deleter', 'SET')::text",
            )
        )

    assert owners == ["standingproof_deleter", "standingproof_deleter"]
    # The hand-over is over: the owner can no longer act as the role that deletes.
    assert can_act_as == ["false"]
