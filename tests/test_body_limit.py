from typing import Any

from standingproof.api.body_limit import BodyLimitMiddleware


async def _never_called(scope: Any, receive: Any, send: Any) -> None:
    raise AssertionError("the application must not run for an oversized request")


async def test_rejects_by_content_length_without_reading_the_body() -> None:
    sent: list[dict[str, Any]] = []
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", b"5000")]}
    await BodyLimitMiddleware(_never_called, max_bytes=1000)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert reads == 0


async def test_stops_reading_a_body_that_lies_about_or_omits_its_length() -> None:
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 400, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        pass

    async def read_everything(scope: Any, receive: Any, send: Any) -> None:
        while True:  # a handler that would read forever if nothing stopped it
            await receive()

    scope = {"type": "http", "method": "POST", "headers": []}
    try:
        await BodyLimitMiddleware(read_everything, max_bytes=1000)(scope, receive, send)
    except Exception as error:
        assert getattr(error, "status_code", None) == 413
    else:
        raise AssertionError("expected the read to be cut off")

    assert reads == 3  # 400 + 400 + 400 passes 1000 on the third chunk
