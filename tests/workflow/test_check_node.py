"""The checker as a step of the graph. State in memory, scripted turns, no network."""

import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError
from standingproof.agent.prompts import CHECKER_PROMPT
from standingproof.agent.types import AnswerDraft
from standingproof.workflow.graph import build_graph, run_question
from standingproof.workflow.state import ExactMatch, Outcome, QuestionContext
from tests.agent.fakes import (
    ScriptedChecker,
    ScriptedModel,
    checker_says,
    disputes,
    finishes,
    search,
    stops,
    usage,
    wants_tools,
)

MFA_ID = "LIB-MFA-01"
QUESTION = "Is multi-factor authentication enforced for all employees?"
DRAFT = AnswerDraft(
    status="answered",
    answer="Yes. MFA is required.",
    citations=[MFA_ID],
    note="DRAFTER-NOTE the library answer fits",
)
SECOND = DRAFT.model_copy(update={"answer": "Yes. MFA is required for all staff."})
NO_SOURCE = AnswerDraft(status="no_approved_source", answer="", citations=[], note="none")
REDRAFT = {"decision": "redraft", "instruction": "REVIEWER-WORDS say staff"}


async def no_match(question: str) -> ExactMatch | None:
    return None


class Rig:
    def __init__(self, model: Any, checker: Any, **overrides: Any) -> None:
        self.graph = build_graph(InMemorySaver())
        self.values: dict[str, Any] = {
            "model": model,
            "checker": checker,
            "registry": build_registry(LIBRARY),
            "limits": Limits(),
            "find_exact": no_match,
            **overrides,
        }

    async def run(self, resume: dict[str, str] | None = None) -> Outcome:
        return await run_question(
            self.graph,
            thread_id="q1",
            question=QUESTION,
            context=QuestionContext(**self.values),
            resume=resume,
        )


def drafter(*extra: Any) -> ScriptedModel:
    return ScriptedModel([wants_tools(search("c1", "MFA")), finishes(DRAFT), *extra])


async def test_a_draft_the_checker_agrees_with_is_ready() -> None:
    checker = ScriptedChecker([checker_says("supported")])

    outcome = await Rig(drafter(), checker).run()

    assert (outcome.status, outcome.path, outcome.reason) == ("ready", "drafted", None)
    assert outcome.check is not None
    assert outcome.check.outcome == "agrees"
    assert outcome.check.answer_checked == DRAFT.answer
    assert len(checker.requests) == 1


async def test_a_draft_the_checker_disputes_goes_to_a_person_with_the_draft_kept() -> None:
    checker = ScriptedChecker([checker_says("not_supported", disputes("MFA is required"))])

    outcome = await Rig(drafter(), checker).run()

    assert (outcome.status, outcome.path) == ("needs_review", "drafted")
    assert outcome.reason == "checker_disagrees"
    assert outcome.draft == DRAFT
    assert outcome.check is not None
    assert [f.quote for f in outcome.check.findings] == ["MFA is required"]


async def test_a_checker_reply_that_did_not_finish_leaves_the_draft_unchecked() -> None:
    checker = ScriptedChecker([checker_says(None, stop_reason="refusal")])

    outcome = await Rig(drafter(), checker).run()

    assert (outcome.status, outcome.reason) == ("needs_review", "not_checked")
    assert outcome.draft == DRAFT


async def test_the_checker_is_sent_nothing_of_how_the_draft_was_made() -> None:
    checker = ScriptedChecker([checker_says("supported")])
    model = drafter()

    await Rig(model, checker).run()

    [request] = checker.requests
    assert request["system"] == CHECKER_PROMPT
    sent = json.dumps(request["messages"])
    # What it must see.
    assert QUESTION in sent
    assert DRAFT.answer in sent
    assert MFA_ID in sent
    # What it must not: the drafter's instructions, its tool calls and results as messages,
    # its search query as a call, and its note.
    assert [m["role"] for m in request["messages"]] == ["user"]
    assert "tool_use" not in sent
    assert "tool_result" not in sent
    assert "search_approved_answers" not in sent
    assert "DRAFTER-NOTE" not in sent
    assert set(request) == {"system", "messages"}


async def test_a_reused_answer_is_not_checked() -> None:
    async def matched(question: str) -> ExactMatch | None:
        return ExactMatch(source_id=MFA_ID, answer="Yes. Approved wording.")

    checker = ScriptedChecker([])

    outcome = await Rig(ScriptedModel([]), checker, find_exact=matched).run()

    assert (outcome.status, outcome.path) == ("ready", "reused")
    assert outcome.check is None
    assert checker.requests == []


@pytest.mark.parametrize(
    "turns", [[stops("refusal")], [wants_tools(search("c1", "zzz")), finishes(NO_SOURCE)]]
)
async def test_a_question_with_no_accepted_wording_is_not_checked(turns: list[Any]) -> None:
    checker = ScriptedChecker([])

    outcome = await Rig(ScriptedModel(turns), checker).run()

    assert outcome.status == "needs_review"
    assert outcome.check is None
    assert checker.requests == []


async def test_with_no_checker_a_draft_is_ready_as_before() -> None:
    outcome = await Rig(drafter(), None).run()

    assert (outcome.status, outcome.check) == ("ready", None)


async def test_a_failure_that_may_pass_is_raised_and_the_retry_does_not_draft_again() -> None:
    checker = ScriptedChecker(
        [ModelCallError("rate limited", retryable=True), checker_says("supported")]
    )
    model = drafter()
    rig = Rig(model, checker)

    with pytest.raises(ModelCallError):
        await rig.run()
    outcome = await rig.run()

    assert outcome.status == "ready"
    assert outcome.draft == DRAFT
    assert len(model.requests) == 2  # the two drafter turns of the first attempt, no more
    assert len(checker.requests) == 2


async def test_a_failure_that_will_not_pass_leaves_the_draft_unchecked() -> None:
    checker = ScriptedChecker([ModelCallError("bad request")])

    outcome = await Rig(drafter(), checker).run()

    assert (outcome.status, outcome.reason) == ("needs_review", "not_checked")


async def test_a_checked_question_is_not_checked_again_when_run_again() -> None:
    checker = ScriptedChecker([checker_says("supported")])
    rig = Rig(drafter(), checker)

    first = await rig.run()
    second = await rig.run()

    assert second == first
    assert len(checker.requests) == 1


async def test_the_checks_cost_is_added_and_its_call_is_not_a_step() -> None:
    plain = await Rig(drafter(), None).run()
    checked = await Rig(drafter(), ScriptedChecker([checker_says("supported")])).run()

    assert checked.check is not None
    assert checked.steps == plain.steps == 2
    assert checked.cost_usd == pytest.approx(plain.cost_usd + checked.check.cost_usd)
    assert checked.usage.input_tokens == plain.usage.input_tokens + 1000


async def test_at_the_cost_limit_the_checker_is_not_called() -> None:
    model = ScriptedModel(
        [wants_tools(search("c1", "MFA")), finishes(DRAFT, usage(input_tokens=1_000_000))]
    )
    checker = ScriptedChecker([])

    outcome = await Rig(model, checker, limits=Limits(max_cost_usd=1.0)).run()

    assert (outcome.status, outcome.reason) == ("needs_review", "not_checked")
    assert outcome.draft == DRAFT
    assert checker.requests == []


async def test_a_redraft_is_checked_again_and_never_shown_the_reviewers_words() -> None:
    checker = ScriptedChecker(
        [checker_says("not_supported", disputes("MFA is required")), checker_says("supported")]
    )
    rig = Rig(drafter(finishes(SECOND)), checker)

    first = await rig.run()
    second = await rig.run(resume=REDRAFT)

    assert first.reason == "checker_disagrees"
    assert (second.status, second.reason) == ("ready", None)
    assert second.check is not None
    assert second.check.answer_checked == SECOND.answer
    assert len(checker.requests) == 2
    assert "REVIEWER-WORDS" not in json.dumps(checker.requests[1])


async def test_a_question_saved_before_the_checker_existed_is_checked_on_its_redraft() -> None:
    rig = Rig(drafter(finishes(SECOND)), None)
    await rig.run()  # paused at review, saved with no check in its state

    checker = ScriptedChecker([checker_says("supported")])
    rig.values["checker"] = checker
    outcome = await rig.run(resume=REDRAFT)

    assert outcome.draft == SECOND
    assert outcome.check is not None
    assert outcome.check.outcome == "agrees"
