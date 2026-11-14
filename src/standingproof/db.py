from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(database_url: str) -> AsyncEngine:
    """Create the process-wide connection pool.

    pool_pre_ping tests a pooled connection before handing it out, so a connection the
    database has closed is replaced instead of failing a request. The connect timeout keeps
    a dead database from hanging callers.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 2},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the factory that hands out one session per request.

    expire_on_commit=False keeps loaded objects readable after commit. The default would
    reload each attribute on next access, which in async code is a hidden database call.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
