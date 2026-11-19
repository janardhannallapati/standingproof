"""A structured reply with no tools, against a stand-in for the SDK client. No network."""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from standingproof.agent.loop import ModelCallError
from standingproof.agent.model import AnthropicStructured

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class Colour(BaseModel):
    name: str


class Messages:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._reply = reply
        self._error = error

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._reply


def reply(stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=Colour(name="green"),
        usage=SimpleNamespace(
            input_tokens=700,
            output_tokens=30,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


async def ask(messages: Messages) -> Any:
    client: Any = SimpleNamespace(messages=messages)
    reader = AnthropicStructured(client, model="claude-sonnet-5-5", effort="low", output=Colour)
    return await reader.ask(system="instructions", messages=[{"role": "user", "content": "x"}])


async def test_the_call_carries_no_tools_and_asks_for_the_given_shape() -> None:
    messages = Messages(reply())

    turn = await ask(messages)

    [call] = messages.calls
    assert "tools" not in call
    assert "tool_choice" not in call
    assert call["output_format"] is Colour
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"] == {"effort": "low"}
    assert call["max_tokens"] == 2_000
    assert call["system"] == [
        {"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}}
    ]
    assert call["messages"] == [{"role": "user", "content": "x"}]
    assert turn.parsed == Colour(name="green")
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (700, 30)
    assert turn.model == "claude-sonnet-5-5"


async def test_a_reply_that_was_cut_short_carries_nothing() -> None:
    turn = await ask(Messages(reply("max_tokens")))

    assert (turn.stop_reason, turn.parsed) == ("max_tokens", None)


async def test_a_provider_failure_is_the_error_the_callers_know() -> None:
    error = anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, request=REQUEST), body=None
    )

    with pytest.raises(ModelCallError) as caught:
        await ask(Messages(error=error))

    assert caught.value.retryable
