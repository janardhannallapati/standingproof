"""Dependencies that routes declare instead of reaching into global state."""

from typing import Annotated, cast

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
RedisDep = Annotated[Redis, Depends(get_redis)]
