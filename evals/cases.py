"""The eval cases, as data. Loading validates them: a case that cannot be graded is refused.

Two sets. A drafter case is a question and a world (a library, and what GitHub and AWS
read); the right answer follows from the world. A checker case is a draft, its sources and
a person's label of whether the sources support it.
"""

import json
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from standingproof.checks.registry import CHECKS

SETS = Path(__file__).parent / "sets"

# What a case may say each setting reads. The world turns these into the system's reply.
READS: dict[str, frozenset[str]] = {
    "github.org_two_factor": frozenset({"required"}),
    "github.branch_protection": frozenset({"protected", "reviews", "admins"}),
    "github.secret_scanning": frozenset({"enabled"}),
    "aws.root_mfa": frozenset({"enabled"}),
    "aws.password_policy": frozenset({"set", "minimum_length", "max_age_days"}),
    "aws.s3_public_access_block": frozenset({"blocked"}),
    "aws.ebs_default_encryption": frozenset({"encrypted"}),
}

Family = Literal["A", "B", "C", "D", "E", "F"]
System = Literal["github", "aws"]
Failure = Literal["forbidden", "unreachable", "rate_limited"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Answer(_Strict):
    question: str
    answer: str
    control_id: str | None = None


class Policy(_Strict):
    name: str
    text: str


class Library(_Strict):
    # Libraries whose answers and policies this one starts from.
    extends: list[str] = []
    answers: list[Answer] = []
    policies: list[Policy] = []


class Live(_Strict):
    """One setting of the world: what a look at it reads, or how the look fails."""

    check: str
    scope: str
    reads: dict[str, bool | int | None] | None = None
    fails: Failure | None = None

    @model_validator(mode="after")
    def _one_result(self) -> Self:
        if self.check not in CHECKS:
            raise ValueError(f"unknown check {self.check!r}")
        if (self.reads is None) == (self.fails is None):
            raise ValueError("give exactly one of `reads` and `fails`")
        if self.reads is not None and set(self.reads) != READS[self.check]:
            raise ValueError(f"reads for {self.check} must have {sorted(READS[self.check])}")
        return self

    @property
    def owner(self) -> str:
        """The organisation or account a connection must name for this look to be allowed."""
        return self.scope.split("/")[0].lower()

    @property
    def system(self) -> System:
        return CHECKS[self.check].system


class Cite(_Strict):
    check: str
    scope: str


class Expect(_Strict):
    """What a right result is. Patterns are regular expressions, matched ignoring case."""

    outcome: Literal["answered", "no_source"]
    # The setting the case is about. Whether a ready answer cites a look at it is counted
    # in the report; the grade itself rests on the wording.
    cite: Cite | None = None
    # Each of these must be found in a ready answer's wording, and none of `must_not`.
    must: list[str] = []
    must_not: list[str] = []

    @field_validator("must", "must_not")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"pattern {pattern!r} does not compile: {error}") from None
        return patterns

    @model_validator(mode="after")
    def _no_wording_for_no_source(self) -> Self:
        if self.outcome == "no_source" and (self.cite or self.must or self.must_not):
            raise ValueError("a no_source case expects no wording: leave cite, must, must_not out")
        return self


class DrafterCase(_Strict):
    name: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    family: Family
    question: str
    library: str
    connected: dict[System, list[str]] = {}
    live: list[Live] = []
    expect: Expect
    # Family F: the same case without the planted instruction.
    twin: str | None = None
    note: str = ""

    def allowed(self, live: Live) -> bool:
        return live.owner in {scope.lower() for scope in self.connected.get(live.system, [])}

    @model_validator(mode="after")
    def _family_rules(self) -> Self:
        family, expect = self.family, self.expect
        observed = [live for live in self.live if live.reads is not None and self.allowed(live)]
        if family in "AB":
            cite = expect.cite
            seen = {(live.check, live.scope) for live in observed}
            if cite is None or (cite.check, cite.scope) not in seen:
                raise ValueError(f"family {family} must cite a setting its world lets it observe")
        if family == "B" and not expect.must_not:
            raise ValueError("family B needs must_not: the library's claim the world contradicts")
        if family == "C" and (not self.live or observed or expect.outcome != "no_source"):
            raise ValueError("family C is a look that fails or is refused, and expects no_source")
        if family in "DE" and self.live:
            raise ValueError(f"family {family} has no live setting")
        if family == "E" and expect.outcome != "no_source":
            raise ValueError("family E expects no_source")
        if (family == "F") != (self.twin is not None):
            raise ValueError("family F, and only family F, names its twin")
        return self


class DrafterSet(_Strict):
    libraries: dict[str, Library]
    cases: list[DrafterCase]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [case.name for case in self.cases]
        _unique(names)
        for name, library in self.libraries.items():
            if not set(library.extends) <= self.libraries.keys() - {name}:
                raise ValueError(f"library {name!r} extends one that does not exist")
        for case in self.cases:
            if case.library not in self.libraries:
                raise ValueError(f"{case.name}: no library named {case.library!r}")
            if case.twin is not None and case.twin not in names:
                raise ValueError(f"{case.name}: no case named {case.twin!r} to be its twin")
        return self

    def library(self, name: str) -> Library:
        """A library with everything it extends, one level deep, in order."""
        own = self.libraries[name]
        bases = [self.libraries[base] for base in own.extends]
        return Library(
            answers=[a for lib in (*bases, own) for a in lib.answers],
            policies=[p for lib in (*bases, own) for p in lib.policies],
        )

    @property
    def by_name(self) -> dict[str, DrafterCase]:
        return {case.name: case for case in self.cases}


Defect = Literal[
    "none",
    "wider_scope",
    "stronger_word",
    "added_detail",
    "wrong_number",
    "contradicted_uncited",
    "policy_vs_observed",
    "half_answered",
    "planted_instruction",
]


class CheckerCase(_Strict):
    name: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    defect: Defect
    question: str
    answer: str
    citations: list[str]
    sources: dict[str, list[str]]
    # A person's judgement: do the cited sources support this draft as an answer?
    label: Literal["supported", "not_supported"]
    # For a defect, the kind of finding a careful reader would report.
    kind: Literal["unsupported", "contradicted", "not_answered"] | None = None
    # The words of the draft that carry the defect, when it has a place.
    phrase: str = ""

    @model_validator(mode="after")
    def _label_matches(self) -> Self:
        sound = self.defect == "none"
        if sound != (self.label == "supported"):
            raise ValueError("label and defect disagree: only defect 'none' is supported")
        if sound != (self.kind is None):
            raise ValueError("kind is given for a defect and left out for a sound draft")
        if not set(self.citations) <= self.sources.keys():
            raise ValueError("the draft cites a source the case does not give")
        if self.phrase and self.phrase not in self.answer:
            raise ValueError("phrase is not in the draft's wording")
        return self


class CheckerSet(_Strict):
    cases: list[CheckerCase]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _unique([case.name for case in self.cases])
        return self

    @property
    def by_name(self) -> dict[str, CheckerCase]:
        return {case.name: case for case in self.cases}


def _unique(names: list[str]) -> None:
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"case names used more than once: {repeated}")


def load_drafter(path: Path = SETS / "drafter.json") -> DrafterSet:
    return DrafterSet.model_validate(json.loads(path.read_text()))


def load_checker(path: Path = SETS / "checker.json") -> CheckerSet:
    return CheckerSet.model_validate(json.loads(path.read_text()))
