from fastapi import FastAPI

from standingproof import __version__
from standingproof.api import health


def create_app() -> FastAPI:
    app = FastAPI(title="Standingproof", version=__version__)
    app.include_router(health.router)
    return app
