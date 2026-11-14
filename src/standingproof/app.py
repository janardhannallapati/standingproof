from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from standingproof import __version__
from standingproof.api import health, questionnaires
from standingproof.api.body_limit import BodyLimitMiddleware
from standingproof.config import Settings, get_settings
from standingproof.db import build_engine, build_session_factory

_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory (not a module-level `app`) lets each test build its own instance with its own
    settings, and keeps importing this module free of side effects.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Runs once at startup; the code after `yield` runs once at shutdown.
        app.state.engine = build_engine(settings.database_url.get_secret_value())
        app.state.sessions = build_session_factory(app.state.engine)
        app.state.redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="Standingproof", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    # No request body may exceed the largest upload plus room for the multipart framing.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.upload_max_bytes + _MULTIPART_OVERHEAD_BYTES
    )
    app.include_router(health.router)
    app.include_router(questionnaires.router)
    return app
