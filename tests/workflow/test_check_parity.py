"""With a checker, the graph and the hand-written loop must still agree."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError, run_agent
from standingproof.agent.steps import check_reason
from standingproof.agent.types import AnswerDraft, ModelTurn
from standingproof.workflow.graph import build_graph, run_question
from standingproof.workflow.state import ExactMatch, QuestionContext
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


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedModel(ScriptedModel):
    def __init__(self, turns: list[ModelTurn], clock: Clock, seconds: float) -> None:
        super().__init__(turns)
        self._clock = clock
        self._seconds = seconds

    async def complete(self, **kwargs: Any) -> ModelTurn:
        self._clock.now += self._seconds
        return await super().complete(**kwargs)


async def no_match(question: str) -> ExactMatch | None:
    return None


def drafted(**final: Any) -> list[ModelTurn]:
    return [wants_tools(search("c1", "MFA")), finishes(DRAFT, **final)]


# name -> (drafter turns, checker turns, limits)
SCENARIOS: dict[str, tuple[list[ModelTurn], list[Any], Limits]] = {
    "agrees": (drafted(), [checker_says("supported")], Limits()),
    "disagrees": (
        drafted(),
        [checker_says("not_supported", disputes("MFA is required"))],
        Limits(),
    ),
    "supported_with_findings": (
        drafted(),
        [checker_says("supported", disputes("MFA is required"))],
        Limits(),
    ),
    "checker_declined": (drafted(), [checker_says(None, stop_reason="refusal")], Limits()),
    "checker_rejected": (drafted(), [ModelCallError("bad request")], Limits()),
    "cost_limit_before_check": (
        drafted(tokens=usage(input_tokens=1_000_000)),
        [],
        Limits(max_cost_usd=1.0),
    ),
    "time_limit_before_check": (drafted(), [], Limits(max_seconds=60.0)),
    "no_source": ([wants_tools(search("c1", "zzz")), finishes(NO_SOURCE)], [], Limits()),
    "declined": ([stops("refusal")], [], Limits()),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_graph_and_the_loop_agree_with_a_checker(name: str) -> None:
    turns, checks, limits = SCENARIOS[name]

    loop_clock = Clock()
    loop_checker = ScriptedChecker(list(checks))
    by_loop = await run_agent(
        question=QUESTION,
        model=TimedModel(turns, loop_clock, 40.0),
        registry=build_registry(LIBRARY),
        limits=limits,
        clock=loop_clock,
        checker=loop_checker,
    )
    graph_clock = Clock()
    graph_checker = ScriptedChecker(list(checks))
    by_graph = await run_question(
        build_graph(InMemorySaver()),
        thread_id=name,
        question=QUESTION,
        context=QuestionContext(
            model=TimedModel(turns, graph_clock, 40.0),
            registry=build_registry(LIBRARY),
            limits=limits,
            find_exact=no_match,
            clock=graph_clock,
            checker=graph_checker,
        ),
    )

    assert len(graph_checker.requests) == len(loop_checker.requests)
    assert graph_checker.requests == loop_checker.requests
    assert by_graph.steps == by_loop.steps
    assert by_graph.cost_usd == pytest.approx(by_loop.cost_usd)
    assert by_graph.usage == by_loop.usage
    assert by_graph.check == by_loop.check
    if by_loop.handoff is not None:
        assert (by_graph.status, by_graph.reason) == ("needs_review", by_loop.handoff)
    elif by_loop.check is not None:
        assert by_graph.draft == by_loop.draft
        assert by_graph.reason == check_reason(by_loop.check)
        assert by_graph.status == ("ready" if by_graph.reason is None else "needs_review")
