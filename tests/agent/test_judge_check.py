"""What a checker's reply means, decided in code. No model, no network."""

import pytest

from standingproof.agent.steps import check_reason, judge_check, not_checked
from standingproof.agent.types import CheckTurn, CheckVerdict, Finding, Usage

ANSWER = "Yes. MFA is required for all employees and contractors."
SOURCES = {"LIB-1", "LIB-2"}


def finding(
    kind: str = "unsupported",
    quote: str = "and contractors",
    source_id: str = "LIB-1",
    explanation: str = "The source covers employees only.",
) -> Finding:
    return Finding(kind=kind, quote=quote, source_id=source_id, explanation=explanation)  # type: ignore[arg-type]


def turn(
    verdict: str | None,
    *findings: Finding,
    stop_reason: str = "end_turn",
    tokens: Usage | None = None,
) -> CheckTurn:
    return CheckTurn(
        stop_reason=stop_reason,
        verdict=(
            CheckVerdict(verdict=verdict, findings=list(findings))  # type: ignore[arg-type]
            if verdict
            else None
        ),
        usage=tokens or Usage(input_tokens=1000, output_tokens=100),
        model="claude-sonnet-5-5",
    )


def test_supported_with_no_findings_agrees() -> None:
    result = judge_check(turn("supported"), ANSWER, SOURCES)

    assert result.outcome == "agrees"
    assert result.findings == []
    assert result.answer_checked == ANSWER
    assert result.model == "claude-sonnet-5-5"
    assert check_reason(result) is None


def test_the_cost_of_the_call_is_on_the_result() -> None:
    result = judge_check(turn("supported"), ANSWER, SOURCES)

    # 1,000 input at $2 and 100 output at $10, per million.
    assert result.cost_usd == pytest.approx(0.003)


def test_not_supported_disagrees_and_keeps_the_findings() -> None:
    result = judge_check(turn("not_supported", finding()), ANSWER, SOURCES)

    assert result.outcome == "disagrees"
    assert check_reason(result) == "checker_disagrees"
    [found] = result.findings
    assert (found.kind, found.quote, found.located, found.source_id) == (
        "unsupported",
        "and contractors",
        True,
        "LIB-1",
    )


def test_supported_that_still_lists_a_finding_disagrees() -> None:
    result = judge_check(turn("supported", finding()), ANSWER, SOURCES)

    assert result.outcome == "disagrees"
    assert len(result.findings) == 1


def test_not_supported_with_nothing_listed_still_disagrees() -> None:
    result = judge_check(turn("not_supported"), ANSWER, SOURCES)

    assert result.outcome == "disagrees"
    assert result.findings == []


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens", "pause_turn"])
def test_a_reply_that_did_not_finish_is_not_a_check(stop_reason: str) -> None:
    result = judge_check(turn("supported", stop_reason=stop_reason), ANSWER, SOURCES)

    assert result.outcome == "not_checked"
    assert check_reason(result) == "not_checked"


def test_a_reply_with_no_verdict_is_not_a_check() -> None:
    assert judge_check(turn(None), ANSWER, SOURCES).outcome == "not_checked"


def test_a_check_that_was_never_made_costs_nothing() -> None:
    result = not_checked(ANSWER)

    assert (result.outcome, result.cost_usd, result.model) == ("not_checked", 0.0, None)
    assert result.answer_checked == ANSWER


def test_only_the_first_five_findings_are_kept() -> None:
    many = [finding(explanation=f"finding {n}") for n in range(7)]

    result = judge_check(turn("not_supported", *many), ANSWER, SOURCES)

    assert [f.explanation for f in result.findings] == [f"finding {n}" for n in range(5)]


def test_a_long_explanation_is_cut() -> None:
    result = judge_check(turn("not_supported", finding(explanation="x" * 900)), ANSWER, SOURCES)

    assert len(result.findings[0].explanation) == 300


def test_a_quote_that_is_not_in_the_draft_is_kept_but_not_located() -> None:
    result = judge_check(turn("not_supported", finding(quote="and all vendors")), ANSWER, SOURCES)

    assert result.findings[0].located is False
    assert result.findings[0].quote == "and all vendors"


def test_a_quote_is_located_whatever_its_spacing_and_case() -> None:
    result = judge_check(
        turn("not_supported", finding(quote="  And   contractors ")), ANSWER, SOURCES
    )

    assert result.findings[0].located is True
    # Stored as it appears in the draft, so the screen can mark exactly those characters.
    assert result.findings[0].quote == "and contractors"


def test_an_empty_quote_is_not_located() -> None:
    result = judge_check(
        turn("not_supported", finding(kind="not_answered", quote="")), ANSWER, SOURCES
    )

    assert result.findings[0].located is False


def test_a_source_the_checker_was_not_given_is_dropped() -> None:
    result = judge_check(turn("not_supported", finding(source_id="LIB-99")), ANSWER, SOURCES)

    assert result.findings[0].source_id is None


def test_an_empty_source_id_means_no_source() -> None:
    result = judge_check(turn("not_supported", finding(source_id="")), ANSWER, SOURCES)

    assert result.findings[0].source_id is None
