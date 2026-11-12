from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


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
