"""The loop's decisions, one at a time. No model, no tools, no clock."""

import pytest

from standingproof.agent.limits import Limits
from standingproof.agent.steps import (
    TurnDecision,
    add_usage,
    check_draft,
    decide,
    fingerprint,
    limit_reached,
    repeated,
    tool_result_block,
)
from standingproof.agent.tools import ToolOutcome
from standingproof.agent.types import AnswerDraft, HandoffReason, ToolCall, Usage
from tests.agent.fakes import finishes, search, stops, wants_tools

LIMITS = Limits(max_steps=3, max_cost_usd=1.0, max_seconds=60.0)
SOURCES = {"LIB-1": ["MFA is required for all staff."]}


def answered(*citations: str, answer: str = "Yes. MFA is required.") -> AnswerDraft:
    return AnswerDraft(status="answered", answer=answer, citations=list(citations), note="")


@pytest.mark.parametrize(
    ("steps", "spent", "elapsed", "expected"),
    [
        (0, 0.0, 0.0, None),
        (2, 0.99, 59.9, None),
        (3, 0.0, 0.0, HandoffReason.STEP_LIMIT),
        (0, 1.0, 0.0, HandoffReason.COST_LIMIT),
        (0, 0.0, 60.0, HandoffReason.TIME_LIMIT),
        # Steps are checked first, then cost, then time: the order of the original loop.
        (3, 1.0, 60.0, HandoffReason.STEP_LIMIT),
        (0, 1.0, 60.0, HandoffReason.COST_LIMIT),
    ],
)
def test_a_limit_stops_the_run_before_the_next_call(
    steps: int, spent: float, elapsed: float, expected: HandoffReason | None
) -> None:
    assert limit_reached(steps=steps, spent=spent, elapsed=elapsed, limits=LIMITS) == expected


def test_usage_is_added_field_by_field() -> None:
    total = add_usage(
        Usage(input_tokens=1, output_tokens=2, cache_creation_input_tokens=3),
        Usage(input_tokens=10, output_tokens=20, cache_read_input_tokens=40),
    )

    assert total == Usage(
        input_tokens=11,
        output_tokens=22,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=40,
    )


def test_the_same_arguments_in_another_order_are_the_same_call() -> None:
    first = ToolCall(id="a", name="t", arguments={"x": 1, "y": 2})
    second = ToolCall(id="b", name="t", arguments={"y": 2, "x": 1})

    assert fingerprint(first) == fingerprint(second)
    assert repeated([second], {fingerprint(first)})
    assert not repeated([search("c", "other")], {fingerprint(first)})


def test_a_turn_that_asks_for_tools_continues() -> None:
    decision = decide(wants_tools(search("c1", "MFA")), SOURCES)

    assert decision == TurnDecision(kind="tools")


def test_a_finished_turn_with_a_grounded_draft_is_accepted() -> None:
    draft = answered("LIB-1")

    assert decide(finishes(draft), SOURCES) == TurnDecision(kind="done", draft=draft)


def test_a_finished_turn_with_no_draft_is_handed_off() -> None:
    turn = finishes(answered("LIB-1")).model_copy(update={"draft": None})

    assert decide(turn, SOURCES) == TurnDecision(
        kind="handoff", handoff=HandoffReason.UNGROUNDED_ANSWER
    )


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("refusal", HandoffReason.MODEL_DECLINED),
        ("max_tokens", HandoffReason.OUTPUT_TRUNCATED),
        ("pause_turn", HandoffReason.UNEXPECTED_STOP),
    ],
)
def test_any_other_stop_is_handed_off(stop_reason: str, expected: HandoffReason) -> None:
    assert decide(stops(stop_reason), SOURCES) == TurnDecision(kind="handoff", handoff=expected)


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        (answered("LIB-1"), None),
        (answered(), HandoffReason.UNGROUNDED_ANSWER),
        (answered("LIB-404"), HandoffReason.UNGROUNDED_ANSWER),
        (answered("LIB-1", answer="  "), HandoffReason.UNGROUNDED_ANSWER),
        (answered("LIB-1", answer="We are certified."), HandoffReason.UNSUPPORTED_CLAIM),
        (AnswerDraft(status="no_approved_source", answer="", citations=[], note="none"), None),
        (
            AnswerDraft(status="no_approved_source", answer="Yes", citations=[], note=""),
            HandoffReason.UNGROUNDED_ANSWER,
        ),
        (
            AnswerDraft(status="no_approved_source", answer="", citations=["LIB-1"], note=""),
            HandoffReason.UNGROUNDED_ANSWER,
        ),
    ],
)
def test_a_draft_is_accepted_only_when_grounded(
    draft: AnswerDraft, expected: HandoffReason | None
) -> None:
    assert check_draft(draft, SOURCES) == expected


def test_a_tool_result_block_is_matched_to_its_call_and_marks_errors() -> None:
    call = search("c1", "MFA")

    ok = tool_result_block(call, ToolOutcome(content="found"))
    failed = tool_result_block(call, ToolOutcome(content="Error: nope", is_error=True))

    assert ok == {"type": "tool_result", "tool_use_id": "c1", "content": "found"}
    assert failed == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": "Error: nope",
        "is_error": True,
    }
