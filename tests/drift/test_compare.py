"""Which named facts differ between two observations of one setting."""

from standingproof.drift.compare import Change, changed_facts

WAS = {"default_branch": "main", "protected": True, "required_approving_reviews": 2}


def test_the_same_facts_are_no_change() -> None:
    assert changed_facts(WAS, dict(WAS)) == []


def test_each_fact_that_differs_is_listed_by_name_in_order() -> None:
    now = {"default_branch": "main", "protected": False, "required_approving_reviews": 0}

    assert changed_facts(WAS, now) == [
        Change("protected", True, False),
        Change("required_approving_reviews", 2, 0),
    ]


def test_a_fact_that_appears_or_disappears_is_a_change() -> None:
    assert changed_facts({"a": 1}, {"a": 1, "b": 2}) == [Change("b", None, 2)]
    assert changed_facts({"a": 1, "b": 2}, {"a": 1}) == [Change("b", 2, None)]


def test_true_is_not_one_and_two_is_not_the_text_two() -> None:
    # In Python True == 1. Stored facts are JSON, where they are different values.
    assert changed_facts({"a": True}, {"a": 1}) == [Change("a", True, 1)]
    assert changed_facts({"a": 2}, {"a": "2"}) == [Change("a", 2, "2")]
    assert changed_facts({"a": 2}, {"a": 2.0}) == []
