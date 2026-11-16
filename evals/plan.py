"""What a run will do, what it should cost, whether a person agreed, and what it left.

Nothing here calls a model or touches a database.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from evals.cases import CheckerSet, DrafterSet

MANIFEST = "RUN.json"
RUNS = 3
# What one unit has cost on this project so far (spend.md), in US dollars.
EACH = {"drafter": 0.022, "docs_only": 0.015, "checker": 0.0019, "cheaper": 0.0003}


@dataclass(frozen=True)
class Plan:
    """What a run will do, in order. A pair is a case name and which run of it."""

    drafter: list[tuple[str, int]]
    docs_only: list[str]
    checker: list[tuple[str, int]]
    cheaper: list[str]


def plan_for(drafter: DrafterSet, checker: CheckerSet, *, smoke: bool, runs: int = RUNS) -> Plan:
    if smoke:
        families = {case.family: case.name for case in reversed(drafter.cases)}
        defects = {case.defect: case.name for case in reversed(checker.cases)}
        return Plan(
            drafter=[(families[family], 1) for family in sorted(families)],
            docs_only=[],
            checker=[(name, 1) for name in list(defects.values())[::-1][:6]],
            cheaper=[],
        )
    numbers = range(1, runs + 1)
    return Plan(
        # Run by run, so a run that stops early has covered every case it could.
        drafter=[(case.name, n) for n in numbers for case in drafter.cases],
        docs_only=[case.name for case in drafter.cases if case.family in "ABC"],
        checker=[(case.name, n) for n in numbers for case in checker.cases],
        cheaper=[case.name for case in checker.cases],
    )


@dataclass(frozen=True)
class Line:
    what: str
    calls: int
    each: float

    @property
    def total(self) -> float:
        return self.calls * self.each


@dataclass(frozen=True)
class Estimate:
    lines: list[Line]

    @property
    def total(self) -> float:
        return sum(line.total for line in self.lines)


def estimate(plan: Plan) -> Estimate:
    return Estimate(
        [
            Line("drafter cases, each with its check", len(plan.drafter), EACH["drafter"]),
            Line("the same without the check tool", len(plan.docs_only), EACH["docs_only"]),
            Line("labelled drafts read by the checker", len(plan.checker), EACH["checker"]),
            Line("the same read by the cheaper model", len(plan.cheaper), EACH["cheaper"]),
        ]
    )


def approved(
    *, ceiling: float, stated: float | None, terminal: bool, ask: Callable[[str], str]
) -> bool:
    """Whether a person has agreed to spend up to the ceiling. Fails closed.

    Either someone at a terminal types the word, or the caller states the amount they were
    approved for and it is this ceiling.
    """
    if stated is not None:
        return abs(stated - ceiling) < 0.005
    if not terminal:
        return False
    return ask(f"Type yes to spend up to ${ceiling:.2f}: ").strip() == "yes"


class Manifest(BaseModel):
    """What the last run did. An incomplete run cannot be accepted as the baseline."""

    complete: bool
    # Why it stopped early; empty when it did not.
    stopped: str
    spent_usd: float
    recorded: int
    reused: int
    finished_at: str


def load_manifest(root: Path) -> Manifest | None:
    path = root / MANIFEST
    return Manifest.model_validate(json.loads(path.read_text())) if path.exists() else None
