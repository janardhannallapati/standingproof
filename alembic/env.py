"""Alembic's entry point. Runs migrations with the same settings the application uses."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from standingproof.config import get_settings
from standingproof.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Migrations are written by hand. The models are given here so that `alembic check`
# (run by the tests) fails when a model and the migrations disagree.
target_metadata = Base.metadata


_EMBEDDING_TABLES = {"library_answer_embeddings", "policy_chunk_embeddings"}


def _include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    # The per-model vector indexes are expression indexes with a WHERE clause, which the
    # models cannot describe. They are left out of the comparison here and checked by name
    # in tests/test_migrations.py instead.
    return not (type_ == "index" and parent_names.get("table_name") in _EMBEDDING_TABLES)


def _database_url() -> str:
    # Read from Settings, not alembic.ini, so the URL (and its password) lives in one place.
    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (alembic upgrade head --sql)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_name=_include_name
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    # NullPool: a migration run is short-lived and must not keep connections open.
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
