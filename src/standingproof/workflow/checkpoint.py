"""Where the graph's state is saved: Postgres, in a schema of its own.

The checkpoint library creates and migrates its own tables, and speaks to Postgres through
`psycopg` (the rest of the application uses `asyncpg`). Both facts are kept inside this
module. The tables live in schema `workflow`, apart from the tables our migrations own.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from standingproof.db import STATE_ROLE, refusal, unsafe_role_query

SCHEMA = "workflow"

_CONNECTION_OPTIONS = {
    # The library runs each statement on its own; it does not expect an open transaction.
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
    # Its SQL names tables without a schema, so the schema is chosen here.
    "options": f"-c search_path={SCHEMA}",
}


def conninfo(database_url: str) -> str:
    """The application's database URL in the form psycopg takes."""
    scheme, separator, rest = database_url.partition("://")
    if not separator:
        raise ValueError("the database URL has no scheme")
    return f"{scheme.split('+')[0]}://{rest}"


async def setup(database_url: str) -> None:
    """Create the schema and the library's tables. Safe to run on every deploy."""
    async with await AsyncConnection.connect(conninfo(database_url), autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    async with await AsyncConnection.connect(
        conninfo(database_url),
        **_CONNECTION_OPTIONS,  # type: ignore[arg-type]
    ) as conn:
        await AsyncPostgresSaver(conn).setup()  # type: ignore[arg-type]
    # The role the workers keep state as: rows in this schema and nothing else anywhere.
    # It exists once the migrations have run; before that there is nobody to grant to.
    async with await AsyncConnection.connect(conninfo(database_url), autocommit=True) as conn:
        found = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (STATE_ROLE,))
        if await found.fetchone() is not None:
            await conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {STATE_ROLE}")
            await conn.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} "
                f"TO {STATE_ROLE}"
            )


async def _refuse_unsafe_role(conn: AsyncConnection[DictRow]) -> None:
    """Each new connection of the pool must be the state role and nothing more."""
    found = await conn.execute(unsafe_role_query(STATE_ROLE))
    row = await found.fetchone()
    if row is None or next(iter(row.values())):
        raise refusal(STATE_ROLE)


@asynccontextmanager
async def open_checkpointer(
    database_url: str, *, max_connections: int = 4, wait_seconds: float = 10
) -> AsyncIterator[AsyncPostgresSaver]:
    """A saver over a small pool of its own, closed when the block ends.

    The URL must sign in as the state role. Any other login, the owner's included, gets
    no connection, so the pool never opens.
    """
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo(database_url),
        min_size=1,
        max_size=max_connections,
        kwargs=_CONNECTION_OPTIONS,
        configure=_refuse_unsafe_role,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=wait_seconds)
    except BaseException:
        await pool.close()
        raise
    try:
        yield AsyncPostgresSaver(pool)
    finally:
        await pool.close()


async def delete_state(saver: AsyncPostgresSaver, thread_ids: Sequence[str]) -> None:
    """Forget everything saved for these questions. Their results live elsewhere."""
    for thread_id in thread_ids:
        await saver.adelete_thread(thread_id)
