"""Dependencies that routes declare instead of reaching into global state."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from standingproof.checks.service import CheckBackends
from standingproof.config import Settings
from standingproof.db import Database
from standingproof.library.embedding import Embedder
from standingproof.models import Tenant


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_engine(request: Request) -> AsyncEngine:
    """For the readiness probe only: a bare connection names no tenant and sees no rows."""
    return get_database(request).app_engine


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_system_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A session that crosses tenants. For the two things a request does before it has a
    tenant: finding out which tenant it is, and recording a webhook delivery."""
    async with get_database(request).across_tenants() as session:
        yield session


def get_embedder(request: Request) -> Embedder:
    return cast(Embedder, request.app.state.embedder)


def get_backends(request: Request) -> CheckBackends:
    return cast(CheckBackends, request.app.state.backends)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
BackendsDep = Annotated[CheckBackends, Depends(get_backends)]
SystemSessionDep = Annotated[AsyncSession, Depends(get_system_session)]


async def current_tenant(session: SystemSessionDep, settings: SettingsDep) -> uuid.UUID:
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


async def get_session(request: Request, tenant_id: TenantDep) -> AsyncIterator[AsyncSession]:
    """One database session per request, bound to the request's tenant.

    The database shows this session that tenant's rows and no others, whatever a query
    says (see standingproof.db). Code that changes data commits explicitly, before the
    response is built, so a client is never told "created" for something that then fails
    to save. Anything left uncommitted when the request ends is rolled back as the
    session closes.
    """
    async with get_database(request).tenant(tenant_id)() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
