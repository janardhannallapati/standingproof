"""The per-question graph, with state kept in memory. No database, no network."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError, run_agent
from standingproof.agent.steps import plain_content
from standingproof.agent.types import AnswerDraft, HandoffReason, ModelTurn
from standingproof.workflow.graph import build_graph, run_question
from standingproof.workflow.state import ExactMatch, Outcome, QuestionContext, initial_state
from tests.agent.fakes import ScriptedModel, finishes, search, stops, usage, wants_tools

MFA_ID = "LIB-MFA-01"
QUESTION = "Is multi-factor authentication enforced for all employees?"


def answered(*citations: str, answer: str = "Yes. MFA is required.") -> AnswerDraft:
    return AnswerDraft(status="answered", answer=answer, citations=list(citations), note="")


async def no_match(question: str) -> ExactMatch | None:
    return None


def context(model: Any, limits: Limits | None = None, **overrides: Any) -> QuestionContext:
    values: dict[str, Any] = {
        "model": model,
        "registry": build_registry(LIBRARY),
        "limits": limits or Limits(),
        "find_exact": no_match,
        **overrides,
    }
    return QuestionContext(**values)


async def run(ctx: QuestionContext, thread: str = "q1") -> Outcome:
    graph = build_graph(InMemorySaver())
    return await run_question(graph, thread_id=thread, question=QUESTION, context=ctx)


async def test_a_grounded_draft_is_ready_for_review() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))])

    outcome = await run(context(model))

    assert outcome.status == "ready"
    assert outcome.path == "drafted"
    assert outcome.reason is None
    assert outcome.draft is not None
    assert outcome.draft.citations == [MFA_ID]
    assert outcome.steps == 2


async def test_an_exact_match_reuses_the_approved_wording_without_a_model_call() -> None:
    model = ScriptedModel([])

    async def find(question: str) -> ExactMatch | None:
        assert question == QUESTION
        return ExactMatch(source_id="answer:123", answer="Yes. MFA is required for all staff.")

    outcome = await run(context(model, find_exact=find))

    assert model.requests == []
    assert outcome.status == "ready"
    assert outcome.path == "reused"
    assert outcome.draft is not None
    assert outcome.draft.answer == "Yes. MFA is required for all staff."
    assert outcome.draft.citations == ["answer:123"]
    assert outcome.steps == 0
    assert outcome.cost_usd == 0.0


async def test_no_approved_source_goes_to_a_person_with_the_note() -> None:
    draft = AnswerDraft(status="no_approved_source", answer="", citations=[], note="Nothing.")
    model = ScriptedModel([wants_tools(search("c1", "quantum")), finishes(draft)])

    outcome = await run(context(model))

    assert outcome.status == "needs_review"
    assert outcome.reason == "no_approved_source"
    assert outcome.draft is not None
    assert outcome.draft.note == "Nothing."


async def test_a_handoff_goes_to_a_person_with_the_reason_and_no_draft() -> None:
    model = ScriptedModel([finishes(answered("LIB-INVENTED"))])

    outcome = await run(context(model))

    assert outcome.status == "needs_review"
    assert outcome.reason == HandoffReason.UNGROUNDED_ANSWER
    assert outcome.draft is None


async def test_a_provider_failure_that_may_pass_is_raised_not_handed_to_a_person() -> None:
    class Busy:
        async def complete(self, **_: object) -> ModelTurn:
            raise ModelCallError("rate limited", retryable=True)

    with pytest.raises(ModelCallError):
        await run(context(Busy()))


async def test_tool_results_reach_the_model_matched_to_their_calls() -> None:
    model = ScriptedModel(
        [
            wants_tools(search("c1", "MFA"), search("c2", "encryption")),
            finishes(answered(MFA_ID)),
        ]
    )

    await run(context(model))

    messages = model.requests[1]["messages"]
    assert messages[0]["content"].startswith("<question>")
    assert messages[-2]["role"] == "assistant"
    assert [b["tool_use_id"] for b in messages[-1]["content"]] == ["c1", "c2"]


async def test_a_finished_question_is_not_run_again() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))])
    graph = build_graph(InMemorySaver())
    ctx = context(model)

    first = await run_question(graph, thread_id="q1", question=QUESTION, context=ctx)
    second = await run_question(graph, thread_id="q1", question=QUESTION, context=ctx)

    assert second == first
    assert len(model.requests) == 2


# --- The graph and the hand-written loop must agree. ---


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedModel(ScriptedModel):
    """A scripted model whose every call takes a fixed time on a shared clock."""

    def __init__(self, turns: list[ModelTurn], clock: Clock, seconds: float) -> None:
        super().__init__(turns)
        self._clock = clock
        self._seconds = seconds

    async def complete(self, **kwargs: Any) -> ModelTurn:
        self._clock.now += self._seconds
        return await super().complete(**kwargs)


class Down:
    async def complete(self, **_: object) -> ModelTurn:
        raise ModelCallError("bad request")


NO_SOURCE = AnswerDraft(status="no_approved_source", answer="", citations=[], note="none")

SCENARIOS: dict[str, tuple[list[ModelTurn], Limits]] = {
    "answered": ([wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID))], Limits()),
    "no_source": ([wants_tools(search("c1", "zzz")), finishes(NO_SOURCE)], Limits()),
    "step_limit": (
        [wants_tools(search("c1", "a")), wants_tools(search("c2", "b"))],
        Limits(max_steps=2),
    ),
    "cost_limit": (
        [wants_tools(search("c1", "a"), tokens=usage(input_tokens=1_000_000))],
        Limits(max_cost_usd=1.0),
    ),
    "time_limit": ([wants_tools(search("c1", "a"))], Limits(max_seconds=30.0)),
    "repeated_call": (
        [wants_tools(search("c1", "MFA")), wants_tools(search("c2", "MFA"))],
        Limits(),
    ),
    "ungrounded": ([finishes(answered())], Limits()),
    "invented_citation": ([finishes(answered("LIB-NOPE"))], Limits()),
    "unsupported_claim": (
        [wants_tools(search("c1", "MFA")), finishes(answered(MFA_ID, answer="We are certified."))],
        Limits(),
    ),
    "declined": ([stops("refusal")], Limits()),
    "truncated": ([stops("max_tokens")], Limits()),
    "unexpected": ([stops("pause_turn")], Limits()),
    "unknown_tool": (
        [
            wants_tools(search("c1", "MFA").model_copy(update={"name": "nope"})),
            finishes(answered()),
        ],
        Limits(),
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_graph_and_the_loop_agree(name: str) -> None:
    turns, limits = SCENARIOS[name]

    loop_clock = Clock()
    by_loop = await run_agent(
        question=QUESTION,
        model=TimedModel(turns, loop_clock, 40.0),
        registry=build_registry(LIBRARY),
        limits=limits,
        clock=loop_clock,
    )
    graph_clock = Clock()
    by_graph = await run(
        context(TimedModel(turns, graph_clock, 40.0), limits, clock=graph_clock), thread=name
    )

    assert by_graph.steps == by_loop.steps
    assert by_graph.cost_usd == pytest.approx(by_loop.cost_usd)
    assert by_graph.usage == by_loop.usage
    if by_loop.handoff is not None:
        assert by_graph.status == "needs_review"
        assert by_graph.reason == by_loop.handoff
        assert by_graph.draft is None
    else:
        assert by_graph.draft == by_loop.draft
        assert by_graph.path == "drafted"


async def test_the_graph_and_the_loop_agree_when_the_provider_rejects_the_request() -> None:
    by_loop = await run_agent(
        question=QUESTION, model=Down(), registry=build_registry(LIBRARY), limits=Limits()
    )
    by_graph = await run(context(Down()))

    assert by_loop.handoff == HandoffReason.MODEL_ERROR
    assert by_graph.reason == HandoffReason.MODEL_ERROR
    assert by_graph.steps == by_loop.steps == 0


# --- What is saved must survive being saved. ---


def test_a_turn_with_a_thinking_block_survives_the_checkpoint_serialiser() -> None:
    content = [
        {"type": "thinking", "thinking": "Look up MFA.", "signature": "EqQBCkYIBxgC+/==é"},
        {"type": "redacted_thinking", "data": "b3BhcXVl"},
        {"type": "tool_use", "id": "c1", "name": "search", "input": {"query": "MFA ✓"}},
    ]
    state = initial_state(QUESTION)
    state["messages"] = [*state["messages"], {"role": "assistant", "content": content}]

    serde = JsonPlusSerializer()
    restored = serde.loads_typed(serde.dumps_typed(state))

    assert restored == state
    assert restored["messages"][-1]["content"] == content


def test_sdk_objects_become_plain_data_before_they_are_saved() -> None:
    from anthropic.types import TextBlock, ToolUseBlock

    blocks = [
        TextBlock(type="text", text="Searching.", citations=None),
        ToolUseBlock(type="tool_use", id="c1", name="search", input={"query": "MFA"}),
    ]

    plain = plain_content(blocks)

    assert plain[0] == {"type": "text", "text": "Searching."}
    assert plain[1]["input"] == {"query": "MFA"}
    assert all(type(block) is dict for block in plain)
    # Idempotent: already-plain data passes through unchanged.
    assert plain_content(plain) == plain
