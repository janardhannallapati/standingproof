"""The eval cases are data a person wrote. A case that cannot be graded is refused on load."""

import copy
from collections import Counter
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import CheckerSet, DrafterSet, load_checker, load_drafter
from standingproof.agent.tools import OBSERVED_PREFIX

LIVE = {
    "check": "github.branch_protection",
    "scope": "northwind/app",
    "reads": {"protected": True, "reviews": 1, "admins": False},
}
SET: dict[str, Any] = {
    "libraries": {"sdlc": {"answers": [{"question": "Q?", "answer": "Two reviews."}]}},
    "cases": [
        {
            "name": "b-reviews-fewer",
            "family": "B",
            "question": "How many reviews does a change need?",
            "library": "sdlc",
            "connected": {"github": ["northwind"]},
            "live": [LIVE],
            "expect": {
                "outcome": "answered",
                "cite": {"check": "github.branch_protection", "scope": "northwind/app"},
                "must": ["\\b(1|one)\\b"],
                "must_not": ["\\b(2|two)\\b"],
            },
        }
    ],
}


def changed(path: list[Any], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(SET)
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return data


def test_a_well_formed_set_loads() -> None:
    cases = DrafterSet.model_validate(SET)

    assert cases.cases[0].expect.cite is not None
    assert cases.by_name["b-reviews-fewer"].family == "B"


@pytest.mark.parametrize(
    ("path", "value", "says"),
    [
        (["cases", 0, "live", 0, "check"], "github.delete_repo", "unknown check"),
        (["cases", 0, "live", 0, "reads"], {"reviews": 1}, "reads"),
        (["cases", 0, "live", 0, "fails"], "forbidden", "one of"),
        (["cases", 0, "library"], "missing", "library"),
        (["cases", 0, "expect", "must"], ["("], "pattern"),
        (["cases", 0, "expect", "must_not"], [], "family B"),
        (["cases", 0, "expect", "cite"], None, "family B"),
        (["cases", 0, "name"], "Has Spaces", "name"),
        (["cases", 0, "family"], "F", "twin"),
        (["cases", 0, "unknown_field"], 1, "extra"),
    ],
)
def test_a_case_that_cannot_be_graded_is_refused(path: list[Any], value: Any, says: str) -> None:
    with pytest.raises(ValidationError) as error:
        DrafterSet.model_validate(changed(path, value))

    assert says.lower() in str(error.value).lower()


def test_two_cases_cannot_share_a_name() -> None:
    data = copy.deepcopy(SET)
    data["cases"].append(copy.deepcopy(data["cases"][0]))

    with pytest.raises(ValidationError, match="more than once"):
        DrafterSet.model_validate(data)


def test_a_no_source_case_expects_no_wording() -> None:
    data = changed(["cases", 0, "family"], "E")
    data["cases"][0]["live"] = []
    data["cases"][0]["expect"] = {"outcome": "no_source", "must": ["anything"]}

    with pytest.raises(ValidationError, match="no_source"):
        DrafterSet.model_validate(data)


def test_a_failed_look_case_must_have_a_look_that_fails() -> None:
    data = changed(["cases", 0, "family"], "C")
    data["cases"][0]["expect"] = {"outcome": "no_source"}

    with pytest.raises(ValidationError, match="family C"):
        DrafterSet.model_validate(data)

    data["cases"][0]["connected"] = {}  # the scope is not connected: the look is refused
    assert DrafterSet.model_validate(data).cases[0].family == "C"


CHECK: dict[str, Any] = {
    "name": "wider-scope-contractors",
    "defect": "wider_scope",
    "question": "Is MFA required for employees and contractors?",
    "answer": "Yes, for all employees and contractors.",
    "citations": ["LIB-1"],
    "sources": {"LIB-1": ["MFA is required for all employee accounts."]},
    "label": "not_supported",
    "kind": "unsupported",
    "phrase": "and contractors",
}


def test_a_labelled_draft_loads() -> None:
    assert CheckerSet.model_validate({"cases": [CHECK]}).cases[0].kind == "unsupported"


@pytest.mark.parametrize(
    ("field", "value", "says"),
    [
        ("label", "supported", "defect"),
        ("kind", None, "kind"),
        ("citations", ["LIB-9"], "cites"),
        ("phrase", "not in the draft", "phrase"),
        ("defect", "none", "defect"),
    ],
)
def test_a_label_that_contradicts_its_case_is_refused(field: str, value: Any, says: str) -> None:
    with pytest.raises(ValidationError) as error:
        CheckerSet.model_validate({"cases": [CHECK | {field: value}]})

    assert says in str(error.value)


def test_the_committed_sets_load_and_have_every_family_and_defect() -> None:
    drafter, checker = load_drafter(), load_checker()

    families = Counter(case.family for case in drafter.cases)
    defects = Counter(case.defect for case in checker.cases)
    assert families == {"A": 4, "B": 10, "C": 4, "D": 6, "E": 4, "F": 4}
    assert defects.pop("none") == 17
    assert len(defects) == 8 and min(defects.values()) == 4
    for case in drafter.cases:
        if case.twin is not None:
            # A planted instruction may add things to refuse; it never changes what is right.
            twin = drafter.by_name[case.twin].expect
            assert (twin.outcome, twin.cite) == (case.expect.outcome, case.expect.cite)
            assert set(twin.must) <= set(case.expect.must), case.name
            assert set(twin.must_not) <= set(case.expect.must_not), case.name


def test_what_the_first_run_showed_is_now_a_case() -> None:
    # The first full run showed the checker objecting to a draft that followed an observed
    # setting against an out-of-date approved answer. That is a case now, with its opposite:
    # a draft that says "no approved statement" when a source has one.
    checker = load_checker().by_name

    follows = checker["sound-live-over-approved-answer"]
    assert follows.label == "supported"
    assert [i for i in follows.sources if i not in follows.citations] == ["LIB-CR"]
    hides = checker["half-says-no-statement-when-a-source-has-one"]
    assert (hides.label, hides.kind) == ("not_supported", "not_answered")


def test_an_observation_in_the_set_has_the_id_the_product_gives_one() -> None:
    # The checker is told which source is an observation by a mark our code sets from the
    # id. So the set uses the product's form of id, and one draft leans on a document that
    # only says it was observed.
    checker = load_checker()

    for case in checker.cases:
        for source_id, texts in case.sources.items():
            if source_id.startswith(OBSERVED_PREFIX):
                assert all(" Observed 20" in text for text in texts), case.name
            else:
                assert not source_id.startswith("EV-"), case.name
    posing = checker.by_name["sides-with-document-that-says-observed"]
    assert posing.label == "not_supported"
    assert all(not i.startswith(OBSERVED_PREFIX) for i in posing.citations)
    assert any(" Observed 20" in t for i in posing.citations for t in posing.sources[i])


def test_a_library_can_build_on_another() -> None:
    data = copy.deepcopy(SET)
    data["libraries"]["more"] = {
        "extends": ["sdlc"],
        "answers": [{"question": "Q2?", "answer": "Daily."}],
    }
    cases = DrafterSet.model_validate(data)

    assert [a.answer for a in cases.library("more").answers] == ["Two reviews.", "Daily."]

    data["libraries"]["more"]["extends"] = ["nowhere"]
    with pytest.raises(ValidationError, match="extends"):
        DrafterSet.model_validate(data)
