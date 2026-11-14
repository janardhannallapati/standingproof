"""Refuse request bodies over a size limit while they are still arriving."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    """Cap the size of every HTTP request body.

    A route cannot do this for an upload: the framework reads and stores the whole body
    before the route function runs. This sits in front of that read. A request that
    declares too large a body is refused before any of it is read; one that declares
    nothing, or lies, is cut off as soon as the bytes received pass the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    def _too_large(self) -> str:
        return f"The request body is larger than the limit of {self._max_bytes} bytes."

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            response = JSONResponse({"detail": self._too_large()}, status_code=413)
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    # Raised inside the framework's body read; its exception handling
                    # turns this into the 413 response.
                    raise HTTPException(status_code=413, detail=self._too_large())
            return message

        await self._app(scope, limited_receive, send)
