"""The agent loop, written by hand.

The model only proposes. This code owns every decision: whether another call is allowed,
whether a tool may run, and whether a final answer is acceptable.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

from standingproof.agent.checker import CheckerClient, run_check
from standingproof.agent.limits import Limits
from standingproof.agent.pricing import cost_usd
from standingproof.agent.prompts import SYSTEM_PROMPT, question_message
from standingproof.agent.steps import (
    add_usage,
    decide,
    fingerprint,
    limit_reached,
    not_checked,
    repeated,
    tool_result_block,
)
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import (
    AgentResult,
    AnswerDraft,
    CheckResult,
    HandoffReason,
    ModelTurn,
    Usage,
)


class ModelCallError(Exception):
    """The provider could not be reached or rejected the request, after the client's retries.

    `retryable` says whether trying again later could work (a rate limit, an outage) or
    not (a request the provider will always reject).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelClient(Protocol):
    """The one thing the loop needs from a model provider."""

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_addendum: str | None = None,
    ) -> ModelTurn:
        """One model turn. `system_addendum` is trusted text for this call only, sent after
        `system` so that the cached prefix (tools and `system`) stays the same."""
        ...


async def run_agent(
    *,
    question: str,
    model: ModelClient,
    registry: ToolRegistry,
    limits: Limits,
    clock: Callable[[], float] = time.monotonic,
    checker: CheckerClient | None = None,
) -> AgentResult:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question_message(question)}]
    tools = registry.definitions()
    seen_calls: set[str] = set()
    seen_sources: dict[str, list[str]] = {}
    total = Usage()
    spent = 0.0
    steps = 0
    started = clock()

    def stop(
        handoff: HandoffReason | None,
        draft: AnswerDraft | None = None,
        check: CheckResult | None = None,
    ) -> AgentResult:
        return AgentResult(
            draft=draft, handoff=handoff, steps=steps, cost_usd=spent, usage=total, check=check
        )

    while True:
        limit = limit_reached(steps=steps, spent=spent, elapsed=clock() - started, limits=limits)
        if limit is not None:
            return stop(limit)

        try:
            turn = await model.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
        except ModelCallError as error:
            # A provider outage is not a reason to guess. The question goes to a person.
            return stop(getattr(error, "reason", HandoffReason.MODEL_ERROR))
        steps += 1
        total = add_usage(total, turn.usage)
        spent += cost_usd(turn.model, turn.usage)

        decision = decide(turn, seen_sources)
        if decision.kind != "tools":
            draft = decision.draft
            if checker is None or draft is None or draft.status != "answered":
                return stop(decision.handoff, draft)
            # A second reader, given the question, the wording and the sources only.
            if limit_reached(steps=0, spent=spent, elapsed=clock() - started, limits=limits):
                return stop(None, draft, not_checked(draft.answer))
            try:
                check, used = await run_check(
                    checker, question=question, draft=draft, sources=seen_sources
                )
            except ModelCallError:
                return stop(None, draft, not_checked(draft.answer))
            total = add_usage(total, used)
            spent += check.cost_usd
            return stop(None, draft, check)

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
