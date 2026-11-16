"""The pause for a person: the graph stops at review and continues with the decision."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.types import AnswerDraft, HandoffReason
from standingproof.workflow.graph import build_graph, run_question
from standingproof.workflow.state import ExactMatch, Outcome, QuestionContext
from tests.agent.fakes import ScriptedModel, finishes, search, wants_tools

MFA_ID = "LIB-MFA-01"
QUESTION = "Is multi-factor authentication enforced for all employees?"
FIRST = AnswerDraft(status="answered", answer="Yes. MFA is required.", citations=[MFA_ID], note="")
SECOND = AnswerDraft(
    status="answered",
    answer="Yes. MFA is required for all workforce members.",
    citations=[MFA_ID],
    note="",
)
REDRAFT = {"decision": "redraft", "instruction": "Say that it covers all workforce members."}


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


class Rig:
    def __init__(self, model: Any, **overrides: Any) -> None:
        self.graph = build_graph(InMemorySaver())
        self.ctx = context(model, **overrides)

    async def run(self, resume: dict[str, str] | None = None) -> Outcome:
        return await run_question(
            self.graph, thread_id="q1", question=QUESTION, context=self.ctx, resume=resume
        )

    async def paused(self) -> bool:
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": "q1"}})
        return bool(snapshot.interrupts)


async def test_a_drafted_question_stops_at_review_with_its_outcome() -> None:
    rig = Rig(ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST)]))

    outcome = await rig.run()

    assert outcome.status == "ready"
    assert outcome.draft == FIRST
    assert await rig.paused()


async def test_a_question_waiting_for_review_is_not_run_again() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST)])
    rig = Rig(model)
    first = await rig.run()

    again = await rig.run()

    assert again == first
    assert len(model.requests) == 2
    assert await rig.paused()


async def test_a_redraft_sends_the_instruction_to_the_model_and_stops_at_review_again() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST), finishes(SECOND)])
    rig = Rig(model)
    await rig.run()

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.draft == SECOND
    assert outcome.status == "ready"
    assert outcome.steps == 3
    assert await rig.paused()
    messages = model.requests[2]["messages"]
    # The model sees its own earlier answer, then the reviewer's words in their own tags.
    assert messages[-2]["role"] == "assistant"
    assert messages[-1]["role"] == "user"
    assert "<reviewer_instruction>" in messages[-1]["content"]
    assert "all workforce members" in messages[-1]["content"]
    # The questionnaire's text is still the first message, still wrapped as data.
    assert messages[0]["content"].startswith("<question>")


async def test_a_redraft_is_checked_by_the_same_rules_as_a_first_draft() -> None:
    invented = FIRST.model_copy(update={"citations": ["LIB-INVENTED"]})
    rig = Rig(
        ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST), finishes(invented)])
    )
    await rig.run()

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.status == "needs_review"
    assert outcome.reason == HandoffReason.UNGROUNDED_ANSWER
    assert outcome.draft is None


async def test_a_redraft_may_search_again_with_the_same_words() -> None:
    model = ScriptedModel(
        [
            wants_tools(search("c1", "MFA")),
            finishes(FIRST),
            wants_tools(search("c2", "MFA")),
            finishes(SECOND),
        ]
    )
    rig = Rig(model)
    await rig.run()

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.draft == SECOND


async def test_a_redraft_gets_its_own_step_allowance() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST), finishes(SECOND)])
    rig = Rig(model, limits=Limits(max_steps=2))
    first = await rig.run()
    assert first.steps == 2

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.draft == SECOND
    assert outcome.steps == 3  # the total keeps counting; the limit applies per round


async def test_a_reused_answer_can_be_redrafted() -> None:
    async def found(question: str) -> ExactMatch | None:
        return ExactMatch(source_id=MFA_ID, answer="Yes. MFA is required.")

    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(SECOND)])
    rig = Rig(model, find_exact=found)
    first = await rig.run()
    assert first.path == "reused"

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.path == "drafted"
    assert outcome.draft == SECOND
    [only] = model.requests[0]["messages"]
    assert only["role"] == "user"
    assert "<question>" in only["content"] and "<reviewer_instruction>" in only["content"]


async def test_a_redraft_after_a_limit_was_hit_continues_the_conversation() -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(SECOND)])
    rig = Rig(model, limits=Limits(max_steps=1))
    first = await rig.run()
    assert first.reason == HandoffReason.STEP_LIMIT

    outcome = await rig.run(resume=REDRAFT)

    assert outcome.draft == SECOND
    last = model.requests[1]["messages"][-1]
    # The tool results and the instruction travel in one user message, results first.
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][-1]["type"] == "text"
    assert "<reviewer_instruction>" in last["content"][-1]["text"]


async def test_resuming_twice_with_the_same_decision_does_not_redraft_twice() -> None:
    """A worker died after the graph took the decision; the next one passes it again."""
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST), finishes(SECOND)])
    rig = Rig(model)
    await rig.run()
    first = await rig.run(resume=REDRAFT)

    # Nothing new was decided: the question is waiting for review of the second draft.
    again = await rig.run()

    assert again == first
    assert len(model.requests) == 3


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
async def test_any_other_decision_ends_the_question(decision: str) -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(FIRST)])
    rig = Rig(model)
    first = await rig.run()

    outcome = await rig.run(resume={"decision": decision, "instruction": ""})

    assert outcome == first
    assert not await rig.paused()
    assert len(model.requests) == 2
