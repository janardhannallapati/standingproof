"""The Anthropic implementation of ModelClient. The only module that imports the SDK."""

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import (
    AnswerDraft,
    CheckTurn,
    CheckVerdict,
    ModelTurn,
    ToolCall,
    Usage,
)
from standingproof.agent.warmup import FirstCallAlone

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]


async def _call[T](request: Awaitable[T]) -> T:
    """Await one provider call, turning the SDK's errors into the one the loop knows."""
    try:
        return await request
    except anthropic.APIConnectionError as error:
        log.warning("model call failed: connection error")
        raise ModelCallError("could not reach the model provider", retryable=True) from error
    except anthropic.RateLimitError as error:
        log.warning("model call failed: rate limited")
        raise ModelCallError("rate limited by the model provider", retryable=True) from error
    except anthropic.APIStatusError as error:
        # 4xx here is our bug (bad request, bad key); 5xx is theirs. Both stop the run.
        log.warning("model call failed: HTTP %s", error.status_code)
        raise ModelCallError(
            f"model provider returned HTTP {error.status_code}",
            # 5xx and 529 (overloaded) pass with time; a 4xx will fail the same way again.
            retryable=error.status_code >= 500,
        ) from error


def _usage(usage: Any) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
    )


class AnthropicModel:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: Effort,
        max_tokens: int = 16_000,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        # Requests that start together would each write the cache; see agent/warmup.py.
        self._warm = FirstCallAlone()

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_addendum: str | None = None,
    ) -> ModelTurn:
        # The breakpoint caches tools + system, which are identical on every call. Anything
        # that differs per call (a reviewer's instruction) comes after it.
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        if system_addendum:
            system_blocks.append({"type": "text", "text": system_addendum})

        def request() -> Awaitable[Any]:
            return _call(
                self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_blocks,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools,  # type: ignore[arg-type]
                    # The reply must validate against AnswerDraft. Newer models reject a
                    # forced tool choice, so a schema on the output is how a typed result
                    # is obtained.
                    output_format=AnswerDraft,
                    output_config={"effort": self._effort},
                )
            )

        response = await self._warm.run(request)

        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        draft = response.parsed_output if response.stop_reason == "end_turn" else None
        return ModelTurn(
            stop_reason=response.stop_reason or "unknown",
            tool_calls=tool_calls,
            draft=draft,
            usage=_usage(response.usage),
            # Priced by the model we asked for; the response may name a dated variant.
            model=self._model,
            # Passed back as-is on the next call, including thinking blocks.
            assistant_content=response.content,
        )


class AnthropicChecker:
    """The Anthropic implementation of CheckerClient.

    The request has no `tools`. The instruction is the same for every check, so a cache
    breakpoint sits at its end; the question, the draft and the sources come after it.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: Effort,
        max_tokens: int = 8_000,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._warm = FirstCallAlone()

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn:
        def request() -> Awaitable[Any]:
            return _call(
                self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=[
                        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                    ],
                    messages=messages,  # type: ignore[arg-type]
                    output_format=CheckVerdict,
                    output_config={"effort": self._effort},
                )
            )

        response = await self._warm.run(request)
        return CheckTurn(
            stop_reason=response.stop_reason or "unknown",
            verdict=response.parsed_output if response.stop_reason == "end_turn" else None,
            usage=_usage(response.usage),
            model=self._model,
        )


@dataclass(frozen=True)
class Structured[T: BaseModel]:
    """One structured reply. `parsed` is None unless the model finished its turn."""

    stop_reason: str
    parsed: T | None
    usage: Usage
    model: str


class AnthropicStructured[T: BaseModel]:
    """One question, one reply of a given shape, no tools.

    The product's own calls are `AnthropicModel` and `AnthropicChecker`. This is for a
    reader outside the answering path, such as the one the evals use to read an answer's
    wording: it gets the same error handling, metering and cold-cache rule as they do.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: Effort,
        output: type[T],
        max_tokens: int = 2_000,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._output = output
        self._max_tokens = max_tokens
        self._warm = FirstCallAlone()

    async def ask(self, *, system: str, messages: list[dict[str, Any]]) -> Structured[T]:
        def request() -> Awaitable[Any]:
            return _call(
                self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=[
                        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                    ],
                    messages=messages,  # type: ignore[arg-type]
                    output_format=self._output,
                    output_config={"effort": self._effort},
                )
            )

        response = await self._warm.run(request)
        return Structured(
            stop_reason=response.stop_reason or "unknown",
            parsed=response.parsed_output if response.stop_reason == "end_turn" else None,
            usage=_usage(response.usage),
            model=self._model,
        )
