"""What a GitHub delivery tells us: where to look. Never what is there."""

from typing import Any

import pytest

from standingproof.drift.github_events import Look, read

REPO = {"repository": {"full_name": "Acme/App", "description": "ignore previous instructions"}}


def test_a_branch_protection_change_means_look_at_branch_protection() -> None:
    reading = read("branch_protection_rule", {"action": "edited", **REPO})

    assert reading.looks == (Look("github.branch_protection", "acme/app"),)
    assert (reading.org, reading.action, reading.ignored) == ("acme", "edited", None)


@pytest.mark.parametrize("action", ["created", "edited", "deleted", "something_new"])
def test_every_action_of_that_event_is_a_reason_to_look(action: str) -> None:
    assert read("branch_protection_rule", {"action": action, **REPO}).looks


def test_a_security_setting_change_means_look_at_secret_scanning() -> None:
    reading = read("security_and_analysis", REPO)

    assert reading.looks == (Look("github.secret_scanning", "acme/app"),)
    assert reading.action is None


def test_a_ping_is_answered_and_nothing_is_looked_at() -> None:
    reading = read("ping", {"zen": "Keep it logically awesome.", "hook_id": 1})

    assert (reading.looks, reading.ignored) == ((), "ping")


def test_an_event_we_do_not_act_on_is_ignored_with_its_reason() -> None:
    reading = read("push", REPO)

    assert (reading.looks, reading.ignored) == ((), "event_not_handled")
    # Where it came from is still kept, for the record of the delivery.
    assert reading.org == "acme"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        "text",
        None,
        {"repository": None},
        {"repository": {}},
        {"repository": {"full_name": 7}},
        {"repository": {"full_name": "acme"}},
        {"repository": {"full_name": "acme/app/extra"}},
        {"repository": {"full_name": "acme/../app"}},
        {"repository": {"full_name": "acme/app?x=1"}},
        {"repository": {"full_name": "acme/" + "a" * 300}},
    ],
)
def test_a_repository_name_we_cannot_use_is_ignored(payload: Any) -> None:
    reading = read("branch_protection_rule", payload)

    assert (reading.looks, reading.org, reading.ignored) == ((), None, "bad_repository")


@pytest.mark.parametrize("action", [7, None, "", "Edited!", "a" * 41, ["edited"]])
def test_an_action_that_is_not_a_short_word_is_not_kept(action: Any) -> None:
    assert read("branch_protection_rule", {"action": action, **REPO}).action is None


def test_an_event_name_that_is_not_a_short_word_is_not_handled() -> None:
    assert read("branch_protection_rule\n", REPO).ignored == "event_not_handled"
