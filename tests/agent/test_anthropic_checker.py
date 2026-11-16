"""The checker's call to the provider, against a stand-in for the SDK client. No network."""

import asyncio
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from standingproof.agent.loop import ModelCallError
from standingproof.agent.model import AnthropicChecker
from standingproof.agent.types import CheckVerdict

VERDICT = CheckVerdict(verdict="supported", findings=[])
REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


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
        parsed_output=VERDICT,
        usage=SimpleNamespace(
            input_tokens=900,
            output_tokens=40,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


def checker(messages: Messages) -> AnthropicChecker:
    client: Any = SimpleNamespace(messages=messages)
    return AnthropicChecker(client, model="claude-sonnet-5-5", effort="medium")


async def ask(messages: Messages) -> Any:
    return await checker(messages).check(
        system="instructions", messages=[{"role": "user", "content": "x"}]
    )


async def test_the_call_carries_no_tools() -> None:
    messages = Messages(reply())

    await ask(messages)

    [call] = messages.calls
    assert "tools" not in call
    assert "tool_choice" not in call


async def test_the_reply_must_be_a_verdict() -> None:
    messages = Messages(reply())

    turn = await ask(messages)

    [call] = messages.calls
    assert call["output_format"] is CheckVerdict
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"] == {"effort": "medium"}
    assert turn.verdict == VERDICT
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (900, 40)
    assert turn.model == "claude-sonnet-5-5"


async def test_the_instruction_is_the_cached_part_and_nothing_else_is() -> None:
    messages = Messages(reply())

    await ask(messages)

    [call] = messages.calls
    # The instruction is the same for every check, so the breakpoint sits at its end.
    # The question, the draft and the sources differ every time and come after it.
    assert call["system"] == [
        {"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}}
    ]
    assert call["messages"] == [{"role": "user", "content": "x"}]


async def test_tokens_read_from_the_cache_are_counted() -> None:
    cached = reply()
    cached.usage.cache_read_input_tokens = 800
    cached.usage.input_tokens = 100

    turn = await ask(Messages(cached))

    assert (turn.usage.input_tokens, turn.usage.cache_read_input_tokens) == (100, 800)


async def test_a_reply_that_did_not_finish_has_no_verdict() -> None:
    turn = await ask(Messages(reply("refusal")))

    assert turn.stop_reason == "refusal"
    assert turn.verdict is None


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (529, True), (500, True), (400, False), (401, False)]
)
async def test_a_provider_error_says_whether_waiting_could_help(
    status: int, retryable: bool
) -> None:
    response = httpx.Response(status, request=REQUEST)
    kind = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    error = kind("refused", response=response, body=None)

    with pytest.raises(ModelCallError) as caught:
        await ask(Messages(error=error))

    assert caught.value.retryable is retryable


async def test_a_connection_failure_can_be_retried() -> None:
    with pytest.raises(ModelCallError) as caught:
        await ask(Messages(error=anthropic.APIConnectionError(request=REQUEST)))

    assert caught.value.retryable is True


class HeldMessages(Messages):
    """A stand-in whose calls stay in progress until let go."""

    def __init__(self) -> None:
        super().__init__(reply())
        self.running = 0
        self.let_go = asyncio.Event()

    async def parse(self, **kwargs: Any) -> Any:
        self.running += 1
        try:
            await self.let_go.wait()
            return await super().parse(**kwargs)
        finally:
            self.running -= 1


async def test_checks_that_start_together_do_not_all_write_the_cache() -> None:
    messages = HeldMessages()
    one = checker(messages)
    request = {"system": "instructions", "messages": [{"role": "user", "content": "x"}]}

    tasks = [asyncio.create_task(one.check(**request)) for _ in range(3)]
    for _ in range(50):
        await asyncio.sleep(0)

    assert messages.running == 1  # the other two wait for the first to write the cache
    messages.let_go.set()
    await asyncio.gather(*tasks)
    assert len(messages.calls) == 3
