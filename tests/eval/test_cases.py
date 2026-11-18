"""The eval cases are data a person wrote. A case that cannot be graded is refused on load."""

import copy
import re
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
    assert families == {"A": 4, "B": 10, "C": 4, "D": 6, "E": 4, "F": 4, "G": 5}
    assert defects.pop("none") == 19
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
    # An observation outweighs a document only about the same setting in the same place.
    elsewhere = checker.by_name["uncited-observed-another-repository"]
    assert elsewhere.label == "not_supported"
    assert [i.startswith(OBSERVED_PREFIX) for i in elsewhere.citations] == [True]
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


def redraft_case(**changes: Any) -> dict[str, Any]:
    """The set with its one case made a redraft case (family G), then changed."""
    data = copy.deepcopy(SET)
    case = data["cases"][0]
    case["family"] = "G"
    case["redraft"] = {"instruction": "Shorter, please.", "live": [copy.deepcopy(LIVE)]}
    case["redraft"].update(changes.pop("redraft", {}))
    case.update(changes)
    return data


def test_a_redraft_case_loads_and_its_world_can_change() -> None:
    now = copy.deepcopy(LIVE)
    now["reads"] = {"protected": False, "reviews": 0, "admins": False}

    cases = DrafterSet.model_validate(redraft_case(redraft={"live": [now]}))

    [case] = cases.cases
    assert case.redraft is not None and case.redraft.instruction == "Shorter, please."
    assert case.redraft.live[0].reads == {"protected": False, "reviews": 0, "admins": False}
    assert case.live[0].reads != case.redraft.live[0].reads


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (changed(["cases", 0, "family"], "G"), "only family G, has a redraft"),
        (redraft_case(family="B"), "only family G, has a redraft"),
        (redraft_case(redraft={"instruction": ""}), "at least 1 character"),
        # The redraft's world is the whole world again, not only what changed.
        (redraft_case(redraft={"live": []}), "names the same settings"),
    ],
)
def test_a_redraft_case_that_cannot_be_run_is_refused(data: dict[str, Any], says: str) -> None:
    with pytest.raises(ValidationError, match=says):
        DrafterSet.model_validate(data)


def test_a_redraft_must_be_able_to_see_the_setting_it_is_graded_on() -> None:
    gone = copy.deepcopy(LIVE)
    gone.pop("reads")
    gone["fails"] = "unreachable"

    with pytest.raises(ValidationError, match="the redraft's world lets it observe"):
        DrafterSet.model_validate(redraft_case(redraft={"live": [gone]}))


# What a labelled draft's sources must not give away. The checker is shown each source's
# id, so an id that states an order or a value answers the case for it.
LOOK_ID = re.compile(r"^evidence:EV-(BP|SS|2FA|EBS|ROOT|S3|PW)-(\d+)$")
TWO_LOOKS = [
    "sound-later-look-unprotected",
    "earlier-look-against-later",
    "sound-later-look-protected",
    "earlier-look-unprotected-against-later",
]


def _looks(case: Any) -> list[str]:
    return [source_id for source_id in case.sources if source_id.startswith(OBSERVED_PREFIX)]


def test_an_observations_id_names_its_setting_and_a_number_and_nothing_else() -> None:
    for case in load_checker().cases:
        for source_id in _looks(case):
            match = LOOK_ID.match(source_id)
            assert match, f"{case.name}: {source_id}"
            # The number is a label. It must not be a number the source's text states
            # (a count of reviews, a length), or the id repeats the value.
            stated = re.findall(r"(?<![\w:-])\d+(?![\w:-])", " ".join(case.sources[source_id]))
            assert match[2] not in stated, f"{case.name}: {source_id}"


def test_the_number_in_an_id_does_not_say_which_of_two_looks_is_later() -> None:
    checker = load_checker()
    later_is_higher = set()
    for name in TWO_LOOKS:
        earlier, later = (int(LOOK_ID.match(i)[2]) for i in _looks(checker.by_name[name]))  # type: ignore[index]
        later_is_higher.add(later > earlier)

    assert later_is_higher == {True, False}
    assert [c.name for c in checker.cases if len(_looks(c)) > 1] == TWO_LOOKS


def test_two_looks_in_one_draft_carry_the_same_minute() -> None:
    # In the product the time in a source is to the minute. If the two differed here, a
    # checker could order them by the text and never use the look number.
    for name in TWO_LOOKS:
        case = load_checker().by_name[name]
        minutes = {text.rsplit("Observed ", 1)[1] for i in _looks(case) for text in case.sources[i]}
        assert len(minutes) == 1, name


def _says_protected(text: str) -> bool:
    return "is not protected" not in text


def _verdicts(case: Any) -> dict[str, bool]:
    """Whether each way of choosing a source to believe would call this draft supported."""
    claim = case.answer.startswith("Yes")
    reads = {source_id: _says_protected(texts[0]) for source_id, texts in case.sources.items()}
    looks = _looks(case)
    values = list(reads.values())
    return {
        "the look number": reads[looks[-1]] == claim,
        "the earlier look": reads[looks[0]] == claim,
        "the last source": values[-1] == claim,
        "the worse reading": all(values) == claim,
        "most of the sources": (sum(values) * 2 > len(values)) == claim,
    }


def test_only_the_look_number_reads_all_four_drafts_as_they_are_labelled() -> None:
    checker = load_checker()
    wrong: dict[str, list[str]] = {}
    for name in TWO_LOOKS:
        case = checker.by_name[name]
        for rule, supported in _verdicts(case).items():
            if supported != (case.label == "supported"):
                wrong.setdefault(rule, []).append(name)

    assert "the look number" not in wrong
    assert set(wrong) == {
        "the earlier look",
        "the last source",
        "the worse reading",
        "most of the sources",
    }
    # And the later look is not the last source where it reads "protected".
    for name in TWO_LOOKS[2:]:
        case = checker.by_name[name]
        assert not list(case.sources)[-1].startswith(OBSERVED_PREFIX)


def test_a_case_read_for_a_setting_tells_the_drafter_where_that_setting_is() -> None:
    # The drafter learns a repository's name from the question or from the library, never
    # from the case. A case that names it nowhere is declined before any look, on the first
    # draft and the redraft alike, and so says nothing about a setting that changed.
    drafter = load_drafter()
    for case in drafter.cases:
        library = drafter.library(case.library)
        told = " ".join([case.question, library.model_dump_json()])
        for state in case.expect.states:
            assert state.scope in told, (case.name, state.scope)
