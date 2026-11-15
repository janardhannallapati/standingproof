"""The agent loop, written by hand.

The model only proposes. This code owns every decision: whether another call is allowed,
whether a tool may run, and whether a final answer is acceptable.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

from standingproof.agent.limits import Limits
from standingproof.agent.pricing import cost_usd
from standingproof.agent.prompts import SYSTEM_PROMPT, question_message
from standingproof.agent.steps import (
    add_usage,
    decide,
    fingerprint,
    limit_reached,
    repeated,
    tool_result_block,
)
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import AgentResult, AnswerDraft, HandoffReason, ModelTurn, Usage


class ModelCallError(Exception):
    """The provider could not be reached or rejected the request, after the client's retries."""


class ModelClient(Protocol):
    """The one thing the loop needs from a model provider."""

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


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
    seen_sources: dict[str, list[str]] = {}
    total = Usage()
    spent = 0.0
    steps = 0
    started = clock()

    def stop(handoff: HandoffReason | None, draft: AnswerDraft | None = None) -> AgentResult:
        return AgentResult(draft=draft, handoff=handoff, steps=steps, cost_usd=spent, usage=total)

    while True:
        limit = limit_reached(steps=steps, spent=spent, elapsed=clock() - started, limits=limits)
        if limit is not None:
            return stop(limit)

        try:
            turn = await model.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
        except ModelCallError:
            # A provider outage is not a reason to guess. The question goes to a person.
            return stop(HandoffReason.MODEL_ERROR)
        steps += 1
        total = add_usage(total, turn.usage)
        spent += cost_usd(turn.model, turn.usage)

        decision = decide(turn, seen_sources)
        if decision.kind != "tools":
            return stop(decision.handoff, decision.draft)

        if repeated(turn.tool_calls, seen_calls):
            return stop(HandoffReason.REPEATED_CALL)
        seen_calls.update(fingerprint(call) for call in turn.tool_calls)

        # The assistant turn goes back unchanged, then every result in ONE user message.
        messages.append({"role": "assistant", "content": turn.assistant_content})
        results: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            outcome = await registry.execute(call)
            for source_id, text in outcome.sources.items():
                seen_sources.setdefault(source_id, []).append(text)
            results.append(tool_result_block(call, outcome))
        messages.append({"role": "user", "content": results})
