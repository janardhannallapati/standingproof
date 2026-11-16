from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from standingproof import __version__
from standingproof.api import checks, events, health, library, questionnaires, review, runs
from standingproof.api.body_limit import BodyLimitMiddleware
from standingproof.checks.service import CheckBackends, build_backends
from standingproof.config import Settings, get_settings
from standingproof.db import build_engine, build_session_factory
from standingproof.library.embedding import Embedder, build_embedder

_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    backends: CheckBackends | None = None,
) -> FastAPI:
    """Build the application.

    A factory (not a module-level `app`) lets each test build its own instance with its own
    settings, and keeps importing this module free of side effects.
    """
    settings = settings or get_settings()
    # Built here, not in lifespan: a wrong model name or a missing key stops the process
    # at startup with a clear message. The local model itself is loaded on first use.
    embedder = embedder or build_embedder(settings)
    backends = backends or build_backends(settings)

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
            await backends.aclose()
            await embedder.aclose()
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="Standingproof", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.embedder = embedder
    app.state.backends = backends
    # No request body may exceed the largest upload plus room for the multipart framing.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.upload_max_bytes + _MULTIPART_OVERHEAD_BYTES
    )
    app.include_router(health.router)
    app.include_router(questionnaires.router)
    app.include_router(library.router)
    app.include_router(checks.router)
    app.include_router(runs.router)
    app.include_router(review.router)
    app.include_router(events.router)
    return app
