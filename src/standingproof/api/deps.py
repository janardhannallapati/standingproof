"""Dependencies that routes declare instead of reaching into global state."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from standingproof.config import Settings
from standingproof.library.embedding import Embedder
from standingproof.models import Tenant


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request.

    Code that changes data commits explicitly, before the response is built, so a client
    is never told "created" for something that then fails to save. Anything left
    uncommitted when the request ends is rolled back as the session closes.
    """
    sessions = cast(async_sessionmaker[AsyncSession], request.app.state.sessions)
    async with sessions() as session:
        yield session


def get_embedder(request: Request) -> Embedder:
    return cast(Embedder, request.app.state.embedder)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_tenant(session: SessionDep, settings: SettingsDep) -> uuid.UUID:
    """The tenant this request acts for.

    Until sign-in exists this is one development tenant, created on first use. Sign-in
    replaces the body of this function; nothing that depends on it changes.
    """
    if settings.environment == "prod":
        # Fail closed. Without this, a production deployment would serve every caller
        # as one shared tenant, with no authentication at all.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Sign-in is not available in this version."
        )
    name = settings.dev_tenant_name
    lookup = select(Tenant.id).where(Tenant.name == name)
    tenant_id = (await session.execute(lookup)).scalar_one_or_none()
    if tenant_id is None:
        # ON CONFLICT DO NOTHING: two first requests at once both succeed.
        await session.execute(
            insert(Tenant).values(name=name).on_conflict_do_nothing(index_elements=[Tenant.name])
        )
        await session.commit()
        tenant_id = (await session.execute(lookup)).scalar_one()
    return tenant_id


TenantDep = Annotated[uuid.UUID, Depends(current_tenant)]
