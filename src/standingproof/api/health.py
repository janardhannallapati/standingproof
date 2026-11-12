import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from standingproof.api.deps import EngineDep, RedisDep

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)

CheckResult = Literal["ok", "failed"]


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


@router.get("/healthz")
async def healthz() -> Health:
    """Liveness: the process is up. Touches no dependency."""
    return Health()


async def _postgres_ok(engine: EngineDep) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # The cause goes to the server log only; the response never carries error details.
        log.exception("readiness: postgres check failed")
        return False


async def _redis_ok(redis: RedisDep) -> bool:
    try:
        return bool(await redis.ping())
    except Exception:
        log.exception("readiness: redis check failed")
        return False


@router.get("/readyz", responses={503: {"model": Readiness}})
async def readyz(engine: EngineDep, redis: RedisDep, response: Response) -> Readiness:
    """Readiness: can this instance serve traffic? Checks every store it depends on."""
    postgres_ok, redis_ok = await asyncio.gather(_postgres_ok(engine), _redis_ok(redis))
    checks: dict[str, CheckResult] = {
        "postgres": "ok" if postgres_ok else "failed",
        "redis": "ok" if redis_ok else "failed",
    }
    ready = postgres_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "not_ready", checks=checks)
