"""The drafter's call to the provider, against a stand-in for the SDK client. No network."""

import asyncio
from types import SimpleNamespace
from typing import Any

from standingproof.agent.model import AnthropicModel
from standingproof.agent.types import AnswerDraft

DRAFT = AnswerDraft(status="no_approved_source", answer="", citations=[], note="Nothing.")


class Messages:
    """Records each call and keeps it in progress until let go."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.running = 0
        self.let_go = asyncio.Event()

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.running += 1
        try:
            await self.let_go.wait()
        finally:
            self.running -= 1
        return SimpleNamespace(
            stop_reason="end_turn",
            parsed_output=DRAFT,
            content=[],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=2000,
            ),
        )


def model(messages: Messages) -> AnthropicModel:
    client: Any = SimpleNamespace(messages=messages)
    return AnthropicModel(client, model="claude-opus-5-5", effort="medium")


def ask(drafter: AnthropicModel, addendum: str | None = None) -> asyncio.Task[Any]:
    return asyncio.create_task(
        drafter.complete(
            system="instructions",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            system_addendum=addendum,
        )
    )


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_questions_that_start_together_do_not_all_write_the_cache() -> None:
    messages = Messages()
    drafter = model(messages)

    tasks = [ask(drafter) for _ in range(4)]
    await settle()

    assert messages.running == 1  # three wait for the first to write the cache
    messages.let_go.set()
    turns = await asyncio.gather(*tasks)
    assert len(messages.calls) == 4
    assert [turn.draft for turn in turns] == [DRAFT] * 4


async def test_the_cached_part_is_the_instruction_and_an_addendum_comes_after_it() -> None:
    messages = Messages()
    messages.let_go.set()

    await ask(model(messages), addendum="Leave contractors out.")

    [call] = messages.calls
    assert call["system"] == [
        {"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Leave contractors out."},
    ]
