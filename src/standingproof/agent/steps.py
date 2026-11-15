"""The decisions of one agent run, as functions with no state of their own.

The hand-written loop (`agent/loop.py`) and the workflow graph (`workflow/`) both call
these, so the two cannot disagree about when a run stops or which answer is acceptable.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from standingproof.agent.claims import unsupported_claims
from standingproof.agent.limits import Limits
from standingproof.agent.tools import ToolOutcome
from standingproof.agent.types import AnswerDraft, HandoffReason, ModelTurn, ToolCall, Usage

_STOP_TO_HANDOFF = {
    "refusal": HandoffReason.MODEL_DECLINED,
    "max_tokens": HandoffReason.OUTPUT_TRUNCATED,
}


@dataclass(frozen=True)
class TurnDecision:
    """What one model turn means for the run: run its tools, accept its draft, or stop."""

    kind: Literal["tools", "done", "handoff"]
    draft: AnswerDraft | None = None
    handoff: HandoffReason | None = None


def limit_reached(
    *, steps: int, spent: float, elapsed: float, limits: Limits
) -> HandoffReason | None:
    """Checked before every model call, so a ceiling is never exceeded by one more call."""
    if steps >= limits.max_steps:
        return HandoffReason.STEP_LIMIT
    if spent >= limits.max_cost_usd:
        return HandoffReason.COST_LIMIT
    if elapsed >= limits.max_seconds:
        return HandoffReason.TIME_LIMIT
    return None


def add_usage(total: Usage, turn: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + turn.input_tokens,
        output_tokens=total.output_tokens + turn.output_tokens,
        cache_creation_input_tokens=total.cache_creation_input_tokens
        + turn.cache_creation_input_tokens,
        cache_read_input_tokens=total.cache_read_input_tokens + turn.cache_read_input_tokens,
    )


def fingerprint(call: ToolCall) -> str:
    # Sorted keys, so the same arguments in a different order count as the same call.
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"


def repeated(calls: Sequence[ToolCall], seen: Collection[str]) -> bool:
    """Asking the same thing again will return the same thing again."""
    return any(fingerprint(call) in seen for call in calls)


def check_draft(
    draft: AnswerDraft, seen_sources: Mapping[str, Sequence[str]]
) -> HandoffReason | None:
    """Decide in code whether a draft may go forward to review. Fails closed."""
    if draft.status == "no_approved_source":
        # "Nothing found" must be exactly that: no wording, no citations.
        if draft.answer.strip() or draft.citations:
            return HandoffReason.UNGROUNDED_ANSWER
        return None
    if not draft.answer.strip() or not draft.citations:
        return HandoffReason.UNGROUNDED_ANSWER
    if not set(draft.citations) <= seen_sources.keys():
        # A cited id that no tool returned in this run was invented or injected.
        return HandoffReason.UNGROUNDED_ANSWER
    cited_texts = [text for source_id in draft.citations for text in seen_sources[source_id]]
    if unsupported_claims(draft.answer, cited_texts):
        return HandoffReason.UNSUPPORTED_CLAIM
    return None


def decide(turn: ModelTurn, seen_sources: Mapping[str, Sequence[str]]) -> TurnDecision:
    if turn.stop_reason == "end_turn":
        if turn.draft is None:
            return TurnDecision(kind="handoff", handoff=HandoffReason.UNGROUNDED_ANSWER)
        problem = check_draft(turn.draft, seen_sources)
        if problem is not None:
            return TurnDecision(kind="handoff", handoff=problem)
        return TurnDecision(kind="done", draft=turn.draft)
    if turn.stop_reason == "tool_use":
        return TurnDecision(kind="tools")
    reason = _STOP_TO_HANDOFF.get(turn.stop_reason, HandoffReason.UNEXPECTED_STOP)
    return TurnDecision(kind="handoff", handoff=reason)


def tool_result_block(call: ToolCall, outcome: ToolOutcome) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": outcome.content,
    }
    if outcome.is_error:
        block["is_error"] = True
    return block


def plain_content(content: Any) -> Any:
    """A model turn's content as plain JSON values, so it can be saved and sent back.

    The SDK returns objects. What the provider accepts back is the same data as dicts, with
    fields it did not set left out. `parsed_output` is added by the SDK on our side and is
    not part of the wire format.
    """
    if isinstance(content, BaseModel):
        data = content.model_dump(mode="json", exclude_none=True)
        data.pop("parsed_output", None)
        return data
    if isinstance(content, list):
        return [plain_content(item) for item in content]
    return content
