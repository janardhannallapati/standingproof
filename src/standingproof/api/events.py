"""A live connection that says "this run changed". The page then asks for the new state.

A WebSocket is opened by a script on a web page and is not subject to CORS: any site a
reviewer visits could try to open one to this API with the reviewer's own access. So the
`Origin` header is checked against an allow-list before the connection is accepted.
"""

import contextlib
import logging
import uuid
from typing import cast

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from standingproof.api.deps import current_tenant
from standingproof.config import Settings
from standingproof.db import Database
from standingproof.models import Run
from standingproof.workflow.events import CHANGED, channel

log = logging.getLogger(__name__)
router = APIRouter(tags=["review"])

# Close codes in the range applications may define.
ORIGIN_REFUSED = 4403
NOT_FOUND = 4404
NOT_SIGNED_IN = 4401
_SEND_TIMEOUT_SECONDS = 5.0


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    """A browser always sends Origin on a WebSocket. A client that sends none is not a
    browser, and cannot be riding on someone's session."""
    if origin is None:
        return True
    allowed = {part.strip().rstrip("/") for part in settings.allowed_origins.split(",")}
    return origin.rstrip("/") in allowed - {""}


async def _owned(websocket: WebSocket, run_id: uuid.UUID) -> int | None:
    """None if this caller may watch the run; otherwise the code to close with."""
    settings = cast(Settings, websocket.app.state.settings)
    database = cast(Database, websocket.app.state.database)
    async with database.across_tenants() as session:
        try:
            tenant_id = await current_tenant(session, settings)
        except HTTPException:
            return NOT_SIGNED_IN
    async with database.tenant(tenant_id)() as session:
        found = await session.execute(
            select(Run.id).where(Run.tenant_id == tenant_id, Run.id == run_id)
        )
        return None if found.scalar_one_or_none() is not None else NOT_FOUND


@router.websocket("/runs/{run_id}/events")
async def run_events(websocket: WebSocket, run_id: uuid.UUID) -> None:
    settings = cast(Settings, websocket.app.state.settings)
    if not origin_allowed(websocket.headers.get("origin"), settings):
        await websocket.close(code=ORIGIN_REFUSED)
        return
    refusal = await _owned(websocket, run_id)
    if refusal is not None:
        await websocket.close(code=refusal)
        return

    await websocket.accept()
    redis = cast(Redis, websocket.app.state.redis)
    pubsub = redis.pubsub()

    async def forward() -> None:
        await pubsub.subscribe(channel(run_id))
        # Subscribed: from here on nothing is missed. Tell the page so it can fetch once.
        await websocket.send_json({"type": "ready"})
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None:
                # A page that cannot take a message in time is dropped; it will reconnect.
                with anyio.fail_after(_SEND_TIMEOUT_SECONDS):
                    await websocket.send_json({"type": CHANGED})

    try:
        # Two things run until either ends: passing messages on, and noticing a close.
        # A task group cancels the other one and waits for it, whichever way this ends.
        async with anyio.create_task_group() as group:

            async def until_closed() -> None:
                with contextlib.suppress(WebSocketDisconnect):
                    while True:
                        await websocket.receive_text()  # nothing is expected from the page
                group.cancel_scope.cancel()

            group.start_soon(until_closed)
            try:
                await forward()
            except (WebSocketDisconnect, TimeoutError, RedisError, OSError) as error:
                log.info("live connection for run %s ended: %s", run_id, type(error).__name__)
            group.cancel_scope.cancel()
    finally:
        # Shielded: the subscription is released even when this handler is being cancelled.
        with anyio.CancelScope(shield=True), contextlib.suppress(RedisError, OSError):
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        with contextlib.suppress(RuntimeError):
            await websocket.close()
