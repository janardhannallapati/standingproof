"""The steps of the graph. Each takes the saved state and returns only what it changes.

A step can run more than once: after a crash it starts again from its first line, with the
state as it was before. So a step reads, decides and returns; it never depends on having
run exactly once.
"""

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from standingproof.agent.loop import ModelCallError
from standingproof.agent.pricing import cost_usd
from standingproof.agent.prompts import SYSTEM_PROMPT, reviewer_instruction
from standingproof.agent.steps import (
    add_usage,
    decide,
    fingerprint,
    limit_reached,
    plain_content,
    repeated,
    tool_result_block,
)
from standingproof.agent.types import AnswerDraft, HandoffReason, ToolCall, Usage
from standingproof.workflow.state import NO_APPROVED_SOURCE, QuestionContext, QuestionState

Update = dict[str, Any]


def _needs_review(reason: str, draft: AnswerDraft | None = None) -> Update:
    return {
        "outcome": {
            "status": "needs_review",
            # Nothing was written unless the model returned a draft ("no approved source").
            "path": "drafted" if draft else None,
            "reason": reason,
            "draft": draft.model_dump() if draft else None,
        }
    }


def _ready(path: str, draft: AnswerDraft) -> Update:
    return {
        "outcome": {"status": "ready", "path": path, "reason": None, "draft": draft.model_dump()}
    }


async def exact_match(state: QuestionState, runtime: Runtime[QuestionContext]) -> Update:
    """Reuse approved wording when the question is one the company has already answered."""
    match = await runtime.context.find_exact(state["question"])
    if match is None:
        return {}
    draft = AnswerDraft(
        status="answered",
        answer=match.answer,
        citations=[match.source_id],
        note="Reused an approved answer: the question matched it exactly.",
    )
    return _ready("reused", draft)


async def call_model(state: QuestionState, runtime: Runtime[QuestionContext]) -> Update:
    """One model turn, and the decision about what it means."""
    ctx = runtime.context
    began = state["round_start"]
    limit = limit_reached(
        steps=state["steps"] - int(began["steps"]),
        spent=state["cost_usd"] - began["cost_usd"],
        elapsed=state["elapsed"] - began["elapsed"],
        limits=ctx.limits,
    )
    if limit is not None:
        return _needs_review(limit)

    started = ctx.clock()
    try:
        turn = await ctx.model.complete(
            system=SYSTEM_PROMPT, messages=state["messages"], tools=ctx.registry.definitions()
        )
    except ModelCallError as error:
        if error.retryable:
            # Nothing is saved for this step, so the next attempt starts here again.
            raise
        return _needs_review(HandoffReason.MODEL_ERROR)

    update: Update = {
        "steps": state["steps"] + 1,
        "usage": add_usage(Usage(**state["usage"]), turn.usage).model_dump(),
        "cost_usd": state["cost_usd"] + cost_usd(turn.model, turn.usage),
        "elapsed": state["elapsed"] + (ctx.clock() - started),
    }
    decision = decide(turn, state["seen_sources"])
    if decision.kind != "tools":
        # Kept so that a reviewer's redraft can continue from what the model last said.
        update["last_turn"] = plain_content(turn.assistant_content) or None
    if decision.kind == "handoff":
        assert decision.handoff is not None  # noqa: S101 - a handoff always carries its reason
        return update | _needs_review(decision.handoff)
    if decision.kind == "done":
        assert decision.draft is not None  # noqa: S101 - a finished turn always has a draft
        if decision.draft.status == NO_APPROVED_SOURCE:
            return update | _needs_review(NO_APPROVED_SOURCE, decision.draft)
        return update | _ready("drafted", decision.draft)

    if repeated(turn.tool_calls, state["seen_calls"]):
        return update | _needs_review(HandoffReason.REPEATED_CALL)
    return update | {
        "seen_calls": [*state["seen_calls"], *(fingerprint(c) for c in turn.tool_calls)],
        # The assistant turn goes back to the provider unchanged, so it is saved unchanged.
        "messages": [
            *state["messages"],
            {"role": "assistant", "content": plain_content(turn.assistant_content)},
        ],
        "pending_calls": [call.model_dump() for call in turn.tool_calls],
    }


async def run_tools(state: QuestionState, runtime: Runtime[QuestionContext]) -> Update:
    """Run every tool the last turn asked for; all results go back in one message."""
    ctx = runtime.context
    started = ctx.clock()
    seen_sources = {source_id: list(texts) for source_id, texts in state["seen_sources"].items()}
    results: list[dict[str, Any]] = []
    for raw in state["pending_calls"]:
        call = ToolCall(**raw)
        outcome = await ctx.registry.execute(call)
        for source_id, text in outcome.sources.items():
            seen_sources.setdefault(source_id, []).append(text)
        results.append(tool_result_block(call, outcome))
    return {
        "messages": [*state["messages"], {"role": "user", "content": results}],
        "pending_calls": [],
        "seen_sources": seen_sources,
        "elapsed": state["elapsed"] + (ctx.clock() - started),
    }


REDRAFT = "redraft"


def _with_instruction(state: QuestionState, instruction: str) -> list[dict[str, Any]]:
    """The conversation, continued with the reviewer's words as the next user message."""
    text = reviewer_instruction(instruction)
    messages = list(state["messages"])
    if state["last_turn"]:
        return [
            *messages,
            {"role": "assistant", "content": state["last_turn"]},
            {"role": "user", "content": text},
        ]
    # No finished model turn to answer (an exact match, or a limit hit after tools ran).
    # The last message is ours, so the instruction joins it: two user turns in a row are
    # not a conversation.
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        joined: Any = f"{content}\n\n{text}"
    else:
        joined = [*content, {"type": "text", "text": text}]
    return [*messages[:-1], {"role": "user", "content": joined}]


async def review(state: QuestionState, runtime: Runtime[QuestionContext]) -> Update:
    """Stop here until a person decides.

    `interrupt` saves the state and ends the run. When the run is resumed with a decision,
    this function starts again from its first line and `interrupt` returns that decision.
    So nothing may come before it that must not happen twice.
    """
    decision = interrupt({"outcome": state["outcome"]})
    if decision.get("decision") != REDRAFT:
        # Approved, edited or rejected. The record of that is the reviewer's decision row;
        # the graph has nothing more to do.
        return {}
    return {
        "messages": _with_instruction(state, str(decision.get("instruction", ""))),
        "outcome": None,
        "last_turn": None,
        "pending_calls": [],
        # A new round: the same search may be made again, and the limits start over.
        "seen_calls": [],
        "redrafts": state["redrafts"] + 1,
        "round_start": {
            "steps": state["steps"],
            "cost_usd": state["cost_usd"],
            "elapsed": state["elapsed"],
        },
    }
