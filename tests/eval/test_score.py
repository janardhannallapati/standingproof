"""Counts, agreement and the gate, against examples worked by hand."""

import pytest

from evals.cases import load_checker, load_drafter
from evals.graders import GradedCheck, GradedDraft, grade_check, grade_draft
from evals.score import Baseline, gate, kappa, score_checker, score_drafter, wilson
from tests.eval.runs import AGREES, EVIDENCE, LIBRARY, OBJECTS, checked, drafted, looked

DRAFTER = load_drafter()
CHECKER = load_checker()


def test_kappa_on_a_table_worked_by_hand() -> None:
    # 20 both yes, 15 both no, 5 yes/no, 10 no/yes: observed 0.70, by chance 0.50.
    first = [True] * 25 + [False] * 25
    second = [True] * 20 + [False] * 5 + [True] * 10 + [False] * 15

    assert kappa(first, second) == pytest.approx(0.4)
    assert kappa(first, first) == 1.0
    assert kappa([True, True], [True, True]) == 1.0  # nothing to disagree about


def test_the_interval_on_30_of_32_is_wide() -> None:
    low, high = wilson(30, 32)

    assert (low, high) == (pytest.approx(0.799, abs=0.001), pytest.approx(0.983, abs=0.001))
    assert wilson(0, 0) == (0.0, 1.0)


def draft(name: str, grade: str, run: int = 1) -> GradedDraft:
    return GradedDraft(name, run, grade, grade != "safe", None, None, None, None, ())  # type: ignore[arg-type]


def all_right(runs: int = 3) -> list[GradedDraft]:
    return [draft(c.name, "right", n) for c in DRAFTER.cases for n in range(1, runs + 1)]


def test_a_case_wrong_in_any_run_is_a_wrong_case() -> None:
    graded = all_right()
    graded[0] = draft(graded[0].case, "wrong", 1)

    score = score_drafter(DRAFTER, graded, runs=3)

    assert score.wrong_cases == [DRAFTER.cases[0].name]
    assert (score.right_cases, score.cases, score.missing) == (32, 32, [])
    assert score.runs_by_grade == {"right": 95, "safe": 0, "wrong": 1}


def test_a_case_is_right_by_majority_and_a_tie_goes_to_the_worse_grade() -> None:
    name = DRAFTER.cases[0].name
    graded = [g for g in all_right() if g.case != name]

    two_of_three = [draft(name, "right", 1), draft(name, "right", 2), draft(name, "safe", 3)]
    one_of_three = [draft(name, "right", 1), draft(name, "safe", 2), draft(name, "safe", 3)]

    assert score_drafter(DRAFTER, graded + two_of_three, runs=3).right_cases == 32
    assert score_drafter(DRAFTER, graded + one_of_three, runs=3).right_cases == 31
    tie = [draft(name, "right", 1), draft(name, "safe", 2)]
    assert score_drafter(DRAFTER, [g for g in graded if g.run < 3] + tie, runs=2).right_cases == 31


def test_a_case_with_too_few_recordings_is_missing() -> None:
    score = score_drafter(DRAFTER, all_right()[1:], runs=3)

    assert score.missing == [DRAFTER.cases[0].name]


def test_families_are_counted_apart() -> None:
    graded = [
        draft(g.case, "safe", g.run) if g.case.startswith("c-") else g for g in all_right(runs=1)
    ]

    score = score_drafter(DRAFTER, graded, runs=1)

    assert score.by_family["C"] == {"right": 0, "safe": 4, "wrong": 0}
    assert score.by_family["B"] == {"right": 10, "safe": 0, "wrong": 0}


def test_the_checker_is_scored_against_what_the_world_says_about_real_drafts() -> None:
    case = DRAFTER.by_name["b-reviews-two-vs-one"]
    bp = ("github.branch_protection", "harborlight/app")
    ok = "A change needs 1 approving review."
    bad = "Yes. Changes require two approving reviews."
    runs = [
        drafted(case.name, ok, [EVIDENCE], tool_uses=[looked(*bp)], run=1),
        drafted(case.name, bad, [LIBRARY], check=OBJECTS, run=2),
        drafted(case.name, bad, [LIBRARY], check=AGREES, run=3),
        drafted(case.name, ok, [EVIDENCE], tool_uses=[looked(*bp)], check=OBJECTS, run=4),
    ]

    score = score_drafter(DRAFTER, [grade_draft(case, run) for run in runs], runs=1)

    assert score.judge == {
        "wrong_objected": 1,
        "wrong_agreed": 1,
        "sound_agreed": 1,
        "sound_objected": 1,
    }
    assert score.looked == (2, 4)


def check(name: str, objected: bool, run: int = 1) -> GradedCheck:
    defect = CHECKER.by_name[name].label == "not_supported"
    outcome = "disagrees" if objected else "agrees"
    return GradedCheck(name, run, outcome, objected, objected == defect, None, None, (0, 0), ())  # type: ignore[arg-type]


def perfect(runs: int = 3) -> list[GradedCheck]:
    return [
        check(c.name, c.label == "not_supported", n)
        for c in CHECKER.cases
        for n in range(1, runs + 1)
    ]


def test_a_perfect_checker() -> None:
    score = score_checker(CHECKER, perfect(), runs=3)

    assert (score.missed_cases, score.false_alarm_cases) == ([], [])
    assert score.matrix == {"caught": 32, "missed": 0, "false_alarm": 0, "passed": 16}
    assert score.kappa == 1.0


def test_a_defect_agreed_with_in_one_run_of_three_is_missed() -> None:
    graded = perfect()
    first_defect = next(i for i, g in enumerate(graded) if g.objected)
    graded[first_defect] = check(graded[first_defect].case, False, 1)

    score = score_checker(CHECKER, graded, runs=3)

    assert score.missed_cases == [graded[first_defect].case]
    # By majority it was still caught, so the table and kappa do not move.
    assert score.matrix["caught"] == 32


def test_a_sound_draft_objected_to_by_majority_is_a_false_alarm() -> None:
    graded = perfect()
    name = "sound-backups"
    graded = [check(name, g.run < 3, g.run) if g.case == name else g for g in graded]

    score = score_checker(CHECKER, graded, runs=3)

    assert score.false_alarm_cases == [name]
    assert score.matrix == {"caught": 32, "missed": 0, "false_alarm": 1, "passed": 15}
    assert 0.9 < score.kappa < 1.0


def test_three_simple_checkers_to_compare_with() -> None:
    score = score_checker(CHECKER, perfect(), runs=3)

    assert score.baselines["always object"] == {"missed": 0, "false_alarms": 16}
    assert score.baselines["always agree"] == {"missed": 32, "false_alarms": 0}
    # The code rule knows four words. It catches the one draft that says "compliant".
    rule = score.baselines["the code rule alone"]
    assert rule["false_alarms"] == 0 and 28 <= rule["missed"] <= 31


def test_kinds_and_quotes_are_counted_over_caught_defects() -> None:
    case = CHECKER.by_name["wider-contractors"]
    from tests.eval.runs import finding, verdict

    right_kind = checked(case.name, verdict(finding("unsupported", "and contractors")), run=1)
    wrong_kind = checked(case.name, verdict(finding("contradicted", "identity provider")), run=2)
    graded = [grade_check(case, right_kind), grade_check(case, wrong_kind)]

    score = score_checker(CHECKER, graded, runs=1)

    assert (score.kind_ok, score.phrase_hit, score.located) == ((1, 2), (1, 2), (2, 2))


BASE = Baseline(
    runs=3,
    drafter_wrong_cases=["b-org-2fa-off"],
    drafter_right_cases=28,
    checker_missed_cases=[],
    checker_false_alarm_cases=2,
    accepted_on="2026-11-16",
    note="first full run",
)


def scores(wrong: list[str], right: int, missed: list[str], alarms: int):  # type: ignore[no-untyped-def]
    d = score_drafter(DRAFTER, all_right(), runs=3)
    c = score_checker(CHECKER, perfect(), runs=3)
    d = d.__class__(**{**d.__dict__, "wrong_cases": wrong, "right_cases": right})
    c = c.__class__(
        **{
            **c.__dict__,
            "missed_cases": missed,
            "false_alarm_cases": [f"s{i}" for i in range(alarms)],
        }
    )
    return d, c


@pytest.mark.parametrize(
    ("wrong", "right", "missed", "alarms", "fails_on"),
    [
        (["b-org-2fa-off"], 28, [], 2, None),  # equal
        ([], 30, [], 0, None),  # better
        (["b-org-2fa-off"], 27, [], 3, None),  # each soft count one case worse
        (["b-org-2fa-off"], 26, [], 2, "right"),
        (["b-org-2fa-off"], 28, [], 4, "objects to"),
        (["b-root-mfa-off"], 28, [], 2, "b-root-mfa-off"),  # a different wrong case
        (["b-org-2fa-off"], 28, ["wider-contractors"], 2, "wider-contractors"),
    ],
)
def test_the_gate(
    wrong: list[str], right: int, missed: list[str], alarms: int, fails_on: str | None
) -> None:
    problems = gate(BASE, *scores(wrong, right, missed, alarms))

    if fails_on is None:
        assert problems == []
    else:
        assert len(problems) == 1 and fails_on in problems[0]


def test_the_gate_refuses_missing_recordings_and_a_different_number_of_runs() -> None:
    drafter = score_drafter(DRAFTER, all_right()[1:], runs=3)
    checker = score_checker(CHECKER, perfect(), runs=3)

    assert any("no recording" in p for p in gate(BASE, drafter, checker))
    two = score_drafter(DRAFTER, all_right(2), runs=2), score_checker(CHECKER, perfect(2), runs=2)
    assert any("runs" in p for p in gate(BASE, *two))


def test_a_check_that_did_not_finish_is_not_a_catch_the_gate_accepts() -> None:
    graded = [
        GradedCheck(g.case, g.run, "not_checked", True, g.correct, None, None, (0, 0), ())
        if g.objected
        else g
        for g in perfect()
    ]
    drafter = score_drafter(DRAFTER, all_right(), runs=3)
    checker = score_checker(CHECKER, graded, runs=3)
    clean = BASE.model_copy(update={"drafter_wrong_cases": [], "drafter_right_cases": 32})

    assert checker.missed_cases == [] and checker.not_checked_runs == 96
    assert any("did not finish" in problem for problem in gate(clean, drafter, checker))


def test_three_copies_of_one_run_are_not_three_runs() -> None:
    name = DRAFTER.cases[0].name
    graded = [g for g in all_right() if g.case != name] + [draft(name, "right", 1)] * 3

    assert score_drafter(DRAFTER, graded, runs=3).missing == [name]
    first = CHECKER.cases[0].name
    checks = [g for g in perfect() if g.case != first] + [check(first, False, 2)] * 3
    assert score_checker(CHECKER, checks, runs=3).missing == [first]
