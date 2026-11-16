"""Test doubles for the model. The loop's logic is tested without any network call."""

from typing import Any

from standingproof.agent.types import (
    AnswerDraft,
    CheckTurn,
    CheckVerdict,
    Finding,
    ModelTurn,
    ToolCall,
    Usage,
)


class ScriptedModel:
    """Returns pre-written turns in order and records every request it received."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_addendum: str | None = None,
    ) -> ModelTurn:
        # Copy the list: the loop keeps appending to its own.
        self.requests.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": tools,
                "system_addendum": system_addendum,
            }
        )
        if not self._turns:
            raise AssertionError("the loop called the model more times than the test scripted")
        return self._turns.pop(0)


def usage(input_tokens: int = 100, output_tokens: int = 20) -> Usage:
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def wants_tools(*calls: ToolCall, tokens: Usage | None = None) -> ModelTurn:
    return ModelTurn(
        stop_reason="tool_use",
        tool_calls=list(calls),
        draft=None,
        usage=tokens or usage(),
        model="claude-opus-5-5",
        assistant_content=[{"type": "tool_use", "id": c.id, "name": c.name} for c in calls],
    )


def finishes(draft: AnswerDraft, tokens: Usage | None = None) -> ModelTurn:
    return ModelTurn(
        stop_reason="end_turn",
        tool_calls=[],
        draft=draft,
        usage=tokens or usage(),
        model="claude-opus-5-5",
        assistant_content=[{"type": "text", "text": draft.model_dump_json()}],
    )


def stops(reason: str) -> ModelTurn:
    return ModelTurn(
        stop_reason=reason,
        tool_calls=[],
        draft=None,
        usage=usage(),
        model="claude-opus-5-5",
        assistant_content=[],
    )


def search(call_id: str, query: str) -> ToolCall:
    return ToolCall(id=call_id, name="search_approved_answers", arguments={"query": query})


class ScriptedChecker:
    """Returns pre-written checker turns in order, or raises one that is an exception."""

    def __init__(self, turns: list[CheckTurn | Exception]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn:
        self.requests.append({"system": system, "messages": list(messages)})
        if not self._turns:
            raise AssertionError("the checker was called more times than the test scripted")
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def checker_says(
    verdict: str | None, *findings: Finding, stop_reason: str = "end_turn"
) -> CheckTurn:
    return CheckTurn(
        stop_reason=stop_reason,
        verdict=(
            CheckVerdict(verdict=verdict, findings=list(findings))  # type: ignore[arg-type]
            if verdict
            else None
        ),
        usage=Usage(input_tokens=1000, output_tokens=100),
        model="claude-sonnet-5-5",
    )


def disputes(quote: str, source_id: str = "LIB-MFA-01") -> Finding:
    return Finding(
        kind="unsupported", quote=quote, source_id=source_id, explanation="Not in the source."
    )
