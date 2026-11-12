"""The agent loop, written by hand.

The model only proposes. This code owns every decision: whether another call is allowed,
whether a tool may run, and whether a final answer is acceptable.
"""

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from standingproof.agent.limits import Limits
from standingproof.agent.pricing import cost_usd
from standingproof.agent.prompts import SYSTEM_PROMPT, question_message
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import (
    AgentResult,
    AnswerDraft,
    HandoffReason,
    ModelTurn,
    ToolCall,
    Usage,
)


class ModelClient(Protocol):
    """The one thing the loop needs from a model provider."""

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


_STOP_TO_HANDOFF = {
    "refusal": HandoffReason.MODEL_DECLINED,
    "max_tokens": HandoffReason.OUTPUT_TRUNCATED,
}


def _fingerprint(call: ToolCall) -> str:
    # Sorted keys, so the same arguments in a different order count as the same call.
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"


def _add(total: Usage, turn: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + turn.input_tokens,
        output_tokens=total.output_tokens + turn.output_tokens,
        cache_creation_input_tokens=total.cache_creation_input_tokens
        + turn.cache_creation_input_tokens,
        cache_read_input_tokens=total.cache_read_input_tokens + turn.cache_read_input_tokens,
    )


def _grounded(draft: AnswerDraft, seen_sources: set[str]) -> bool:
    """An answer must cite at least one source, and only sources a tool returned this run."""
    if draft.status == "no_approved_source":
        return True
    return bool(draft.citations) and set(draft.citations) <= seen_sources


async def run_agent(
    *,
    question: str,
    model: ModelClient,
    registry: ToolRegistry,
    limits: Limits,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question_message(question)}]
    tools = registry.definitions()
    seen_calls: set[str] = set()
    seen_sources: set[str] = set()
    total = Usage()
    spent = 0.0
    steps = 0
    started = clock()

    def stop(handoff: HandoffReason | None, draft: AnswerDraft | None = None) -> AgentResult:
        return AgentResult(draft=draft, handoff=handoff, steps=steps, cost_usd=spent, usage=total)

    while True:
        # Limits are checked before the call, so a ceiling is never exceeded by one more call.
        if steps >= limits.max_steps:
            return stop(HandoffReason.STEP_LIMIT)
        if spent >= limits.max_cost_usd:
            return stop(HandoffReason.COST_LIMIT)
        if clock() - started >= limits.max_seconds:
            return stop(HandoffReason.TIME_LIMIT)

        turn = await model.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
        steps += 1
        total = _add(total, turn.usage)
        spent += cost_usd(turn.model, turn.usage)

        if turn.stop_reason == "end_turn":
            if turn.draft is None or not _grounded(turn.draft, seen_sources):
                return stop(HandoffReason.UNGROUNDED_ANSWER)
            return stop(None, turn.draft)

        if turn.stop_reason != "tool_use":
            return stop(_STOP_TO_HANDOFF.get(turn.stop_reason, HandoffReason.UNEXPECTED_STOP))

        fingerprints = [_fingerprint(call) for call in turn.tool_calls]
        if any(f in seen_calls for f in fingerprints):
            # Asking the same thing again will return the same thing again.
            return stop(HandoffReason.REPEATED_CALL)
        seen_calls.update(fingerprints)

        # The assistant turn goes back unchanged, then every result in ONE user message.
        messages.append({"role": "assistant", "content": turn.assistant_content})
        results: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            outcome = await registry.execute(call)
            seen_sources.update(outcome.source_ids)
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": outcome.content,
            }
            if outcome.is_error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})
