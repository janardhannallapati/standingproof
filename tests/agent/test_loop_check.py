"""The hand-written loop with a checker. Scripted turns, no network."""

import pytest

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError, run_agent
from standingproof.agent.types import AgentResult, AnswerDraft, ModelTurn
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
DRAFT = AnswerDraft(status="answered", answer="Yes. MFA is required.", citations=[MFA_ID], note="")
NO_SOURCE = AnswerDraft(status="no_approved_source", answer="", citations=[], note="none")


def drafted() -> list[ModelTurn]:
    return [wants_tools(search("c1", "MFA")), finishes(DRAFT)]


async def run(
    turns: list[ModelTurn], checker: ScriptedChecker | None, limits: Limits | None = None
) -> AgentResult:
    return await run_agent(
        question=QUESTION,
        model=ScriptedModel(turns),
        registry=build_registry(LIBRARY),
        limits=limits or Limits(),
        checker=checker,
    )


async def test_an_accepted_draft_is_checked_once() -> None:
    checker = ScriptedChecker([checker_says("supported")])

    result = await run(drafted(), checker)

    assert result.draft == DRAFT
    assert result.check is not None
    assert result.check.outcome == "agrees"
    assert len(checker.requests) == 1


async def test_a_disputed_draft_is_returned_with_the_findings() -> None:
    checker = ScriptedChecker([checker_says("not_supported", disputes("MFA is required"))])

    result = await run(drafted(), checker)

    assert result.draft == DRAFT
    assert result.handoff is None
    assert result.check is not None
    assert result.check.outcome == "disagrees"
    assert result.check.findings[0].quote == "MFA is required"


async def test_the_checks_cost_is_in_the_total_and_its_call_is_not_a_step() -> None:
    without = await run(drafted(), None)
    with_check = await run(drafted(), ScriptedChecker([checker_says("supported")]))

    assert with_check.check is not None
    assert with_check.steps == without.steps == 2
    assert with_check.cost_usd == pytest.approx(without.cost_usd + with_check.check.cost_usd)
    assert with_check.usage.input_tokens == without.usage.input_tokens + 1000


async def test_with_no_checker_nothing_is_checked() -> None:
    assert (await run(drafted(), None)).check is None


@pytest.mark.parametrize(
    "turns",
    [
        [stops("refusal")],
        [wants_tools(search("c1", "zzz")), finishes(NO_SOURCE)],
        [finishes(DRAFT.model_copy(update={"citations": ["LIB-NOPE"]}))],
    ],
)
async def test_a_run_with_no_accepted_wording_is_not_checked(turns: list[ModelTurn]) -> None:
    checker = ScriptedChecker([])

    result = await run(turns, checker)

    assert result.check is None
    assert checker.requests == []


async def test_at_the_cost_limit_the_checker_is_not_called() -> None:
    turns = [wants_tools(search("c1", "MFA")), finishes(DRAFT, usage(input_tokens=1_000_000))]
    checker = ScriptedChecker([])

    result = await run(turns, checker, Limits(max_cost_usd=1.0))

    assert result.draft == DRAFT
    assert result.check is not None
    assert (result.check.outcome, result.check.cost_usd) == ("not_checked", 0.0)
    assert checker.requests == []


async def test_a_checker_that_cannot_be_called_leaves_the_draft_unchecked() -> None:
    checker = ScriptedChecker([ModelCallError("bad request")])

    result = await run(drafted(), checker)

    assert result.draft == DRAFT
    assert result.check is not None
    assert result.check.outcome == "not_checked"
