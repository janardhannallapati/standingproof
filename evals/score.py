"""Counts over graded runs, two small statistics, and the gate against the baseline.

Every number here is a count out of a total. With a few dozen cases a percentage hides how
little it rests on, so the report prints the count and an interval beside it.
"""

import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from evals.cases import CheckerCase, CheckerSet, DrafterSet
from evals.graders import GradedCheck, GradedDraft
from standingproof.agent.steps import check_draft
from standingproof.agent.types import AnswerDraft

BASELINE = Path(__file__).parent / "baseline.json"
# How many cases a count that moves with noise may worsen by before the gate fails.
SOFT_SLACK = 1
_WORST_FIRST = ("wrong", "safe", "right")


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for a proportion. Honest for small totals."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def kappa(first: Sequence[bool], second: Sequence[bool]) -> float:
    """Cohen's kappa: agreement between two yes/no raters beyond what chance would give."""
    total = len(first)
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / total
    yes_a, yes_b = sum(first) / total, sum(second) / total
    by_chance = yes_a * yes_b + (1 - yes_a) * (1 - yes_b)
    if by_chance == 1:
        return 1.0  # both said the same thing every time; there was nothing to disagree on
    return (observed - by_chance) / (1 - by_chance)


def _majority(grades: Sequence[str]) -> str:
    """The most common grade; on a tie, the worse one."""
    counts = Counter(grades)
    return max(_WORST_FIRST, key=lambda grade: (counts[grade], -_WORST_FIRST.index(grade)))


def _by_case[G: (GradedDraft, GradedCheck)](graded: Sequence[G]) -> dict[str, list[G]]:
    grouped: dict[str, list[G]] = {}
    for item in graded:
        grouped.setdefault(item.case, []).append(item)
    return grouped


def _missing[G: (GradedDraft, GradedCheck)](
    names: Sequence[str], grouped: dict[str, list[G]], runs: int
) -> list[str]:
    """Cases that do not have exactly runs 1 to `runs`, each once."""
    wanted = list(range(1, runs + 1))
    return [name for name in names if sorted(g.run for g in grouped.get(name, [])) != wanted]


@dataclass(frozen=True)
class DrafterScore:
    cases: int
    runs: int
    # Cases right in most of their runs.
    right_cases: int
    # Cases that were ready and wrong in any run.
    wrong_cases: list[str]
    runs_by_grade: dict[str, int]
    by_family: dict[str, dict[str, int]]
    # Of the answered drafts in cases about a live setting, how many cite a look at it.
    looked: tuple[int, int]
    # The checker against what the world says about the drafter's own wording.
    judge: dict[str, int]
    # Cases with fewer recordings than `runs`.
    missing: list[str]


def score_drafter(cases: DrafterSet, graded: Sequence[GradedDraft], *, runs: int) -> DrafterScore:
    grouped = _by_case(graded)
    family_of = {case.name: case.family for case in cases.cases}
    by_family = {family: dict.fromkeys(("right", "safe", "wrong"), 0) for family in "ABCDEF"}
    for item in graded:
        by_family[family_of[item.case]][item.grade] += 1
    asked = [g for g in graded if g.looked is not None]
    judge = dict.fromkeys(("wrong_objected", "wrong_agreed", "sound_agreed", "sound_objected"), 0)
    for item in graded:
        if item.wording_ok is None or item.check not in ("agrees", "disagrees"):
            continue
        wording = "sound" if item.wording_ok else "wrong"
        judge[f"{wording}_{'agreed' if item.check == 'agrees' else 'objected'}"] += 1
    return DrafterScore(
        cases=len(cases.cases),
        runs=runs,
        right_cases=sum(
            _majority([g.grade for g in grouped.get(case.name, [])] or ["safe"]) == "right"
            for case in cases.cases
        ),
        wrong_cases=[
            case.name
            for case in cases.cases
            if any(g.grade == "wrong" for g in grouped.get(case.name, []))
        ],
        runs_by_grade={
            grade: sum(g.grade == grade for g in graded) for grade in ("right", "safe", "wrong")
        },
        by_family=by_family,
        looked=(sum(bool(g.looked) for g in asked), len(asked)),
        judge=judge,
        missing=_missing([case.name for case in cases.cases], grouped, runs),
    )


@dataclass(frozen=True)
class CheckerScore:
    cases: int
    runs: int
    # Defects the checker agreed with in any run.
    missed_cases: list[str]
    # Sound drafts it objected to in most runs.
    false_alarm_cases: list[str]
    # By majority of runs: defects caught and missed, sound drafts objected to and passed.
    matrix: dict[str, int]
    kappa: float
    # Per kind of defect: runs in which it was caught, out of runs.
    by_defect: dict[str, tuple[int, int]]
    kind_ok: tuple[int, int]
    phrase_hit: tuple[int, int]
    located: tuple[int, int]
    not_checked_runs: int
    baselines: dict[str, dict[str, int]]
    missing: list[str]


def _rule_objects(case: CheckerCase) -> bool:
    draft = AnswerDraft(status="answered", answer=case.answer, citations=case.citations, note="")
    return check_draft(draft, case.sources) is not None


def _simple(cases: CheckerSet, objects: Sequence[bool]) -> dict[str, int]:
    pairs = list(zip(cases.cases, objects, strict=True))
    return {
        "missed": sum(c.label == "not_supported" and not o for c, o in pairs),
        "false_alarms": sum(c.label == "supported" and o for c, o in pairs),
    }


def _ratio(values: Sequence[bool | None]) -> tuple[int, int]:
    known = [value for value in values if value is not None]
    return sum(known), len(known)


def score_checker(cases: CheckerSet, graded: Sequence[GradedCheck], *, runs: int) -> CheckerScore:
    grouped = _by_case(graded)
    objected: dict[str, bool] = {}
    for case in cases.cases:
        votes = [g.objected for g in grouped.get(case.name, [])]
        # No recording counts as an objection: an unread draft goes to a person.
        objected[case.name] = sum(votes) * 2 > len(votes) if votes else True
    defects = [case for case in cases.cases if case.label == "not_supported"]
    sound = [case for case in cases.cases if case.label == "supported"]
    by_defect: dict[str, tuple[int, int]] = {}
    for case in defects:
        mine = grouped.get(case.name, [])
        caught, total = by_defect.get(case.defect, (0, 0))
        by_defect[case.defect] = (caught + sum(g.objected for g in mine), total + len(mine))
    return CheckerScore(
        cases=len(cases.cases),
        runs=runs,
        missed_cases=[
            c.name for c in defects if any(not g.objected for g in grouped.get(c.name, []))
        ],
        false_alarm_cases=[c.name for c in sound if objected[c.name] and c.name in grouped],
        matrix={
            "caught": sum(objected[c.name] for c in defects),
            "missed": sum(not objected[c.name] for c in defects),
            "false_alarm": sum(objected[c.name] for c in sound),
            "passed": sum(not objected[c.name] for c in sound),
        },
        kappa=kappa(
            [case.label == "not_supported" for case in cases.cases],
            [objected[case.name] for case in cases.cases],
        ),
        by_defect=by_defect,
        kind_ok=_ratio([g.kind_ok for g in graded]),
        phrase_hit=_ratio([g.phrase_hit for g in graded]),
        located=(sum(g.located[0] for g in graded), sum(g.located[1] for g in graded)),
        not_checked_runs=sum(g.outcome == "not_checked" for g in graded),
        baselines={
            "always object": _simple(cases, [True] * len(cases.cases)),
            "always agree": _simple(cases, [False] * len(cases.cases)),
            "the code rule alone": _simple(cases, [_rule_objects(c) for c in cases.cases]),
        },
        missing=_missing([case.name for case in cases.cases], grouped, runs),
    )


class Baseline(BaseModel):
    """The accepted result. Written only by `python -m evals accept`."""

    model_config = ConfigDict(extra="forbid")

    runs: int
    # Named, not counted: a newly wrong case fails even if another was fixed.
    drafter_wrong_cases: list[str]
    drafter_right_cases: int
    checker_missed_cases: list[str]
    checker_false_alarm_cases: int
    # Checks that did not finish. Such a draft goes to a person, but nothing read it.
    checker_not_checked_runs: int = 0
    accepted_on: str
    note: str


def load_baseline(path: Path = BASELINE) -> Baseline | None:
    return Baseline.model_validate(json.loads(path.read_text())) if path.exists() else None


def as_baseline(drafter: DrafterScore, checker: CheckerScore, *, on: str, note: str) -> Baseline:
    return Baseline(
        runs=drafter.runs,
        drafter_wrong_cases=drafter.wrong_cases,
        drafter_right_cases=drafter.right_cases,
        checker_missed_cases=checker.missed_cases,
        checker_false_alarm_cases=len(checker.false_alarm_cases),
        checker_not_checked_runs=checker.not_checked_runs,
        accepted_on=on,
        note=note,
    )


def gate(baseline: Baseline, drafter: DrafterScore, checker: CheckerScore) -> list[str]:
    """Why this result may not replace the baseline's. Empty when it may."""
    problems: list[str] = []
    missing = [*drafter.missing, *checker.missing]
    if missing:
        problems.append(f"no recording, too few, or a run twice, for: {', '.join(missing)}")
    if (drafter.runs, checker.runs) != (baseline.runs, baseline.runs):
        problems.append(f"the baseline was accepted with {baseline.runs} runs per case")
    newly_wrong = sorted(set(drafter.wrong_cases) - set(baseline.drafter_wrong_cases))
    if newly_wrong:
        problems.append(f"ready and wrong, and not so in the baseline: {', '.join(newly_wrong)}")
    newly_missed = sorted(set(checker.missed_cases) - set(baseline.checker_missed_cases))
    if newly_missed:
        problems.append(f"the checker agreed with a planted defect: {', '.join(newly_missed)}")
    if checker.not_checked_runs > baseline.checker_not_checked_runs:
        problems.append(
            f"{checker.not_checked_runs} checks did not finish, against "
            f"{baseline.checker_not_checked_runs} in the baseline: an unread draft is not a catch"
        )
    if drafter.right_cases < baseline.drafter_right_cases - SOFT_SLACK:
        problems.append(
            f"{drafter.right_cases} cases right, against {baseline.drafter_right_cases} "
            f"in the baseline (one case of slack)"
        )
    if len(checker.false_alarm_cases) > baseline.checker_false_alarm_cases + SOFT_SLACK:
        problems.append(
            f"the checker objects to {len(checker.false_alarm_cases)} sound drafts, against "
            f"{baseline.checker_false_alarm_cases} in the baseline (one case of slack)"
        )
    return problems
