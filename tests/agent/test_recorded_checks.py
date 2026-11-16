"""A real checker's replies to six hand-written drafts, recorded once and replayed here.

The replies come from the cassettes. `judge_check` and the request builder are the real
code. To re-record (this calls the paid API): `uv run python scripts/record_checker_cases.py`.

Six inputs show how the model behaved on six inputs. They are not a measure of how often
the checker is right.
"""

from pathlib import Path

import pytest

from standingproof.agent.checker import run_check
from standingproof.agent.recording import ReplayChecker
from standingproof.agent.types import CheckResult
from tests.agent.checker_cases import CASES, Case

CASSETTES = Path(__file__).parent / "cassettes" / "checker"
BY_NAME = {case.name: case for case in CASES}


async def replay(name: str) -> CheckResult:
    case = BY_NAME[name]
    checker = ReplayChecker(CASSETTES / f"{name}.json")
    result, _ = await run_check(
        checker, question=case.question, draft=case.draft, sources=case.sources
    )
    assert checker.calls == 1
    return result


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_each_case_is_judged_as_a_person_would(case: Case) -> None:
    result = await replay(case.name)

    assert result.outcome == case.expected
    assert result.cost_usd < 0.01


async def test_a_wider_scope_than_the_source_is_located_in_the_draft() -> None:
    result = await replay("overstated_scope")

    [found] = result.findings
    assert found.kind == "unsupported"
    assert found.located is True
    assert "contractors" in found.quote
    assert len(found.quote) < len(BY_NAME["overstated_scope"].answer) / 2
    assert found.source_id == "LIB-MFA-01"


async def test_an_uncited_live_check_that_says_otherwise_is_reported() -> None:
    result = await replay("contradicted_by_live_check")

    assert {found.kind for found in result.findings} == {"contradicted"}
    assert {found.source_id for found in result.findings} == {"EV-0001"}


async def test_the_unanswered_half_of_a_question_is_reported() -> None:
    result = await replay("half_answered")

    [found] = result.findings
    assert (found.kind, found.located) == ("not_answered", False)


async def test_an_instruction_planted_in_a_source_does_not_buy_agreement() -> None:
    planted = await replay("planted_instruction")
    plain = await replay("overstated_scope")

    assert planted.outcome == "disagrees"
    assert [f.quote for f in planted.findings] == [f.quote for f in plain.findings]
