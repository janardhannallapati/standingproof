"""A wording graded by a recorded reading: a labelled sentence, and a real redraft."""

from typing import Any

import pytest

from evals.cases import load_drafter
from evals.graders import grade_draft, grade_reading
from evals.reader import ReadTurn
from evals.score import score_drafter
from tests.eval.readings import APP, BP, CUT, NOW_OFF, OFF, STILL_TWO, read, sentence
from tests.eval.runs import AGREES, EVIDENCE, OBJECTS, drafted, looked

DRAFTER = load_drafter()


# Grading a sentence


def test_a_sentence_is_graded_by_the_same_comparison_as_a_real_answer() -> None:
    case = DRAFTER.by_name[OFF]
    current, stale = sentence(), sentence("does_not")

    assert grade_reading(current, case, read(current, NOW_OFF)).correct
    assert not grade_reading(current, case, read(current, STILL_TWO)).correct
    assert grade_reading(stale, case, read(stale, STILL_TWO)).correct
    passed = grade_reading(stale, case, read(stale, NOW_OFF))
    assert (passed.correct, passed.outcome) == (False, "states_now")
    unread = grade_reading(stale, case, read(stale, CUT))
    assert (unread.correct, unread.outcome) == (False, "not_read")
    assert grade_reading(stale, case, read(stale, None)).outcome == "not_read"


# Grading a real answer


def redraft(reading: ReadTurn | None, check: Any = AGREES) -> Any:
    return drafted(
        OFF,
        "None are enforced: main is not protected.",
        [EVIDENCE],
        tool_uses=[looked(BP, APP, "evidence:first"), looked(BP, APP)],
        check=check,
        redraft_from=1,
        reading=reading,
    )


def test_a_ready_redraft_whose_reading_matches_the_world_is_right() -> None:
    graded = grade_draft(DRAFTER.by_name[OFF], redraft(NOW_OFF))

    assert (graded.grade, graded.wording_ok, graded.looked, graded.read) == (
        "right",
        True,
        True,
        True,
    )


def test_a_ready_redraft_read_as_stating_the_earlier_value_is_wrong() -> None:
    graded = grade_draft(DRAFTER.by_name[OFF], redraft(STILL_TWO))

    assert (graded.grade, graded.wording_ok) == ("wrong", False)
    assert graded.reasons[0] == "says reviews of github.branch_protection reads 2; it reads 0"


def test_the_same_stale_wording_is_safe_when_the_checker_objected() -> None:
    assert grade_draft(DRAFTER.by_name[OFF], redraft(STILL_TWO, OBJECTS)).grade == "safe"


@pytest.mark.parametrize("reading", [None, CUT])
def test_an_answer_nobody_read_is_not_graded_right_and_is_named(reading: ReadTurn | None) -> None:
    graded = grade_draft(DRAFTER.by_name[OFF], redraft(reading))

    assert (graded.grade, graded.wording_ok, graded.read) == ("safe", None, False)
    score = score_drafter(DRAFTER, [graded], runs=1)
    assert score.unread == [OFF]


def test_a_case_graded_by_patterns_needs_no_reading() -> None:
    run = drafted(
        "a-reviews-two", "Two approving reviews.", [EVIDENCE], tool_uses=[looked(BP, APP)]
    )

    assert grade_draft(DRAFTER.by_name["a-reviews-two"], run).read is None


def test_declining_is_safe_and_needs_no_reading() -> None:
    graded = grade_draft(DRAFTER.by_name[OFF], drafted(OFF))

    assert (graded.grade, graded.read) == ("safe", None)
