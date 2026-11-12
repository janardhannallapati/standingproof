import pytest

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import run_agent
from standingproof.agent.types import AnswerDraft, HandoffReason, ToolCall
from tests.agent.fakes import ScriptedModel, finishes, search, stops, usage, wants_tools

MFA_ID = "LIB-MFA-01"
QUESTION = "Is multi-factor authentication enforced for all employees?"


def answered(*citations: str) -> AnswerDraft:
    return AnswerDraft(
        status="answered", answer="Yes. MFA is required.", citations=list(citations), note=""
    )


async def run(model: ScriptedModel, limits: Limits | None = None):  # type: ignore[no-untyped-def]
    return await run_agent(
        question=QUESTION,
        model=model,
        registry=build_registry(LIBRARY),
        limits=limits or Limits(),
    )


async def test_answers_after_one_tool_call_and_cites_what_the_tool_returned() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))])

    result = await run(model)

    assert result.handoff is None
    assert result.draft is not None
    assert result.draft.citations == [MFA_ID]
    assert result.steps == 2


async def test_tool_result_is_sent_back_matched_to_the_call_id() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))])

    await run(model)

    second_request = model.requests[1]["messages"]
    assert second_request[-2]["role"] == "assistant"
    tool_message = second_request[-1]
    assert tool_message["role"] == "user"
    assert [b["tool_use_id"] for b in tool_message["content"]] == ["c1"]
    assert MFA_ID in tool_message["content"][0]["content"]


async def test_results_of_parallel_calls_go_back_in_one_message() -> None:
    model = ScriptedModel(
        [
            wants_tools(search("c1", "MFA"), search("c2", "encryption")),
            finishes(answered(MFA_ID)),
        ]
    )

    await run(model)

    tool_message = model.requests[1]["messages"][-1]
    assert [b["tool_use_id"] for b in tool_message["content"]] == ["c1", "c2"]


async def test_accepts_no_approved_source_without_citations() -> None:
    draft = AnswerDraft(status="no_approved_source", answer="", citations=[], note="Nothing found.")
    model = ScriptedModel([wants_tools(search("c1", "quantum")), finishes(draft)])

    result = await run(model)

    assert result.handoff is None
    assert result.draft is not None
    assert result.draft.status == "no_approved_source"


async def test_hands_off_when_an_answer_has_no_citation() -> None:
    model = ScriptedModel([finishes(answered())])

    result = await run(model)

    assert result.handoff == HandoffReason.UNGROUNDED_ANSWER
    assert result.draft is None


async def test_hands_off_when_a_citation_was_never_returned_by_a_tool() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered("LIB-MADE-UP"))])

    result = await run(model)

    assert result.handoff == HandoffReason.UNGROUNDED_ANSWER


async def test_stops_at_the_step_limit_and_calls_the_model_no_further() -> None:
    turns = [wants_tools(search(f"c{i}", f"query {i}")) for i in range(10)]
    model = ScriptedModel(turns)

    result = await run(model, Limits(max_steps=3))

    assert result.handoff == HandoffReason.STEP_LIMIT
    assert len(model.requests) == 3


async def test_stops_before_a_call_once_the_spend_ceiling_is_reached() -> None:
    expensive = usage(input_tokens=1_000_000, output_tokens=0)  # $4.00 on Opus 5.5
    model = ScriptedModel(
        [wants_tools(search("c1", "MFA"), tokens=expensive), finishes(answered(MFA_ID))]
    )

    result = await run(model, Limits(max_cost_usd=1.00))

    assert result.handoff == HandoffReason.COST_LIMIT
    assert len(model.requests) == 1
    assert result.cost_usd == pytest.approx(4.00)


async def test_stops_when_the_same_call_is_repeated_with_the_same_arguments() -> None:
    model = ScriptedModel(
        [
            wants_tools(search("c1", "MFA")),
            wants_tools(search("c2", "MFA")),
            finishes(answered(MFA_ID)),
        ]
    )

    result = await run(model)

    assert result.handoff == HandoffReason.REPEATED_CALL
    assert len(model.requests) == 2


async def test_unknown_tool_gets_an_error_result_and_the_loop_continues() -> None:
    rogue = ToolCall(id="c1", name="delete_everything", arguments={})
    model = ScriptedModel(
        [wants_tools(rogue), wants_tools(search("c2", "MFA")), finishes(answered(MFA_ID))]
    )

    result = await run(model)

    error_block = model.requests[1]["messages"][-1]["content"][0]
    assert error_block["is_error"] is True
    assert "delete_everything" in error_block["content"]
    assert result.handoff is None


async def test_invalid_arguments_get_an_error_result_the_model_can_act_on() -> None:
    bad = ToolCall(id="c1", name="search_approved_answers", arguments={"q": "MFA"})
    model = ScriptedModel(
        [wants_tools(bad), wants_tools(search("c2", "MFA")), finishes(answered(MFA_ID))]
    )

    result = await run(model)

    error_block = model.requests[1]["messages"][-1]["content"][0]
    assert error_block["is_error"] is True
    assert "query" in error_block["content"]
    assert result.handoff is None


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("refusal", HandoffReason.MODEL_DECLINED),
        ("max_tokens", HandoffReason.OUTPUT_TRUNCATED),
        ("something_new", HandoffReason.UNEXPECTED_STOP),
    ],
)
async def test_any_stop_that_is_not_a_tool_call_or_a_finish_hands_off(
    stop_reason: str, expected: HandoffReason
) -> None:
    result = await run(ScriptedModel([stops(stop_reason)]))

    assert result.handoff == expected


async def test_the_question_is_wrapped_as_data_not_placed_in_the_instructions() -> None:
    model = ScriptedModel([finishes(answered())])

    await run(model)

    request = model.requests[0]
    assert QUESTION not in request["system"]
    assert f"<question>\n{QUESTION}\n</question>" in request["messages"][0]["content"]


async def test_tools_are_offered_in_a_stable_order_for_prompt_caching() -> None:
    model = ScriptedModel([finishes(answered())])

    await run(model)

    names = [t["name"] for t in model.requests[0]["tools"]]
    assert names == sorted(names)
    assert all(t["strict"] is True for t in model.requests[0]["tools"])


async def test_stops_when_the_time_limit_has_passed() -> None:
    ticks = iter([0.0, 0.0, 500.0])
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))])

    result = await run_agent(
        question=QUESTION,
        model=model,
        registry=build_registry(LIBRARY),
        limits=Limits(max_seconds=120.0),
        clock=lambda: next(ticks),
    )

    assert result.handoff == HandoffReason.TIME_LIMIT
    assert len(model.requests) == 1
