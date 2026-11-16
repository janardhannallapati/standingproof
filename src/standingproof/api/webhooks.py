"""Deliveries from outside systems that say a setting changed.

A delivery is believed for one thing: that whoever sent it holds the webhook secret. What
it says is a place to look, never a fact. The route records it and answers; the watcher
(standingproof.drift) reads the setting itself afterwards.
"""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from standingproof.api.deps import SessionDep, SettingsDep
from standingproof.checks.service import utc_now
from standingproof.drift import deliveries
from standingproof.drift.github_events import read
from standingproof.drift.signature import verify

router = APIRouter(tags=["webhooks"])

_MAX_DELIVERY_CHARS = 100
_MAX_EVENT_CHARS = 60
_DELIVERY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
_EVENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")


class DeliveryOut(BaseModel):
    # The same for a delivery seen for the first time and one seen before.
    accepted: bool = True


async def _body(request: Request, limit: int) -> bytes:
    """The bytes exactly as sent, or 413 as soon as they pass the limit."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The delivery is too large.")
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The delivery is too large.")
    return bytes(received)


def _parsed(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        # Not JSON, or nested too deeply to read. Kept as a delivery we cannot use.
        return None


def _well_formed(value: str, allowed: frozenset[str], longest: int) -> bool:
    return 0 < len(value) <= longest and set(value) <= allowed


@router.post(
    "/webhooks/github",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"description": "The delivery or event header is not in GitHub's form."},
        401: {"description": "The signature does not match."},
        404: {"description": "No webhook secret is configured for this deployment."},
        413: {"description": "The delivery is too large."},
    },
)
async def github_delivery(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    delivery: Annotated[str, Header(alias="X-GitHub-Delivery")] = "",
    event: Annotated[str, Header(alias="X-GitHub-Event")] = "",
) -> DeliveryOut:
    """Record that GitHub says something changed. Answers at once; nothing is looked at here.

    Set the webhook's content type to `application/json` and give it the secret in
    `SP_GITHUB_WEBHOOK_SECRET`. A delivery that arrives twice is recorded once.
    """
    secret = settings.github_webhook_secret
    if secret is None:
        # Without a secret nothing could be told from a forgery, so nothing is accepted.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    body = await _body(request, settings.webhook_max_bytes)
    # Over the bytes as they arrived, and before anything in them is read.
    if not verify(secret.get_secret_value(), body, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "The signature does not match.")
    if not _well_formed(delivery, _DELIVERY_CHARS, _MAX_DELIVERY_CHARS) or not _well_formed(
        event, _EVENT_CHARS, _MAX_EVENT_CHARS
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a GitHub delivery.")
    payload = await asyncio.to_thread(_parsed, body)
    await deliveries.store(
        session, delivery_id=delivery, event=event, reading=read(event, payload), now=utc_now()
    )
    return DeliveryOut()
