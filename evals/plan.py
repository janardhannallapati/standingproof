"""What a run will do, what it should cost, whether a person agreed, and what it left.

Nothing here calls a model or touches a database.
"""

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from evals.cases import CheckerSet, DrafterSet, ReaderSet
from evals.transcript import MANIFEST

RUNS = 3
# What one unit has cost on this project so far (spend.md), in US dollars.
# A redraft case is two rounds, each with its check. Measured on its first full run: twelve
# recordings for $0.5966.
EACH = {
    "drafter": 0.022,
    "redraft": 0.050,
    "docs_only": 0.015,
    "checker": 0.0019,
    "cheaper": 0.0003,
    # A calculation until the first run measures it: about 800 tokens in and 150 out.
    "reader": 0.003,
}


@dataclass(frozen=True)
class Plan:
    """What a run will do, in order. A pair is a case name and which run of it."""

    drafter: list[tuple[str, int]]
    docs_only: list[str]
    checker: list[tuple[str, int]]
    cheaper: list[str]
    # The names in `drafter` that are redraft cases: two rounds each.
    redraft: frozenset[str] = frozenset()
    # Labelled sentences for the reader of wordings: a name and which run.
    reader: list[tuple[str, int]] = dataclasses.field(default_factory=list)


def plan_for(
    drafter: DrafterSet,
    checker: CheckerSet,
    *,
    smoke: bool,
    runs: int = RUNS,
    family: str | None = None,
    reader: ReaderSet | None = None,
    only_reader: bool = False,
) -> Plan:
    redraft = frozenset(case.name for case in drafter.cases if case.redraft is not None)
    # A held-out sentence is read `runs` times; an open one, which the instructions were
    # written against, once.
    readings = [
        (sentence.name, n)
        for n in range(1, runs + 1)
        for sentence in (reader.sentences if reader else [])
        if n == 1 or sentence.part == "held_out"
    ]
    if only_reader:
        return Plan(drafter=[], docs_only=[], checker=[], cheaper=[], reader=readings)
    if family is not None:
        # One kind of drafter case and nothing else: a look at a family before, or without,
        # the full run. The next full run reuses what this recorded, unless the inputs
        # have changed since.
        chosen = [case.name for case in drafter.cases if case.family == family]
        return Plan(
            drafter=[(name, n) for n in range(1, runs + 1) for name in chosen],
            docs_only=[],
            checker=[],
            cheaper=[],
            redraft=redraft & set(chosen),
        )
    if smoke:
        families = {case.family: case.name for case in reversed(drafter.cases)}
        defects = {case.defect: case.name for case in reversed(checker.cases)}
        return Plan(
            drafter=[(families[family], 1) for family in sorted(families)],
            docs_only=[],
            checker=[(name, 1) for name in list(defects.values())[::-1][:6]],
            cheaper=[],
            redraft=redraft & set(families.values()),
        )
    numbers = range(1, runs + 1)
    return Plan(
        # Run by run, so a run that stops early has covered every case it could.
        drafter=[(case.name, n) for n in numbers for case in drafter.cases],
        docs_only=[case.name for case in drafter.cases if case.family in "ABC"],
        checker=[(case.name, n) for n in numbers for case in checker.cases],
        cheaper=[case.name for case in checker.cases],
        redraft=redraft,
        reader=readings,
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
    twice = sum(name in plan.redraft for name, _ in plan.drafter)
    return Estimate(
        [
            Line("drafter cases, each with its check", len(plan.drafter) - twice, EACH["drafter"]),
            Line("redraft cases, two rounds each", twice, EACH["redraft"]),
            Line("the same without the check tool", len(plan.docs_only), EACH["docs_only"]),
            Line("labelled drafts read by the checker", len(plan.checker), EACH["checker"]),
            Line("the same read by the cheaper model", len(plan.cheaper), EACH["cheaper"]),
            Line("labelled sentences read by the reader", len(plan.reader), EACH["reader"]),
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
