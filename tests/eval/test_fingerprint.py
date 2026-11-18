"""A recording is valid only for the inputs that produced it."""

import dataclasses
from typing import Any

import pytest

from evals import fingerprint
from evals.cases import load_checker, load_drafter
from evals.fingerprint import Config, differences, of_checker_case, of_drafter_case

DRAFTER = load_drafter()
CHECKER = load_checker()
CASE = DRAFTER.by_name["b-reviews-two-vs-one"]
CHECK = CHECKER.by_name["wider-contractors"]


def drafter(config: Config | None = None, **changes: Any) -> dict[str, str]:
    cases = DRAFTER
    case = CASE.model_copy(update=changes) if changes else CASE
    return of_drafter_case(cases, case, "full", config or Config.current())


def test_the_same_inputs_give_the_same_fingerprint() -> None:
    assert drafter() == drafter()
    assert differences(drafter(), drafter()) == []
    assert set(drafter()) >= {"drafter.prompt", "drafter.tools", "checker.prompt", "case"}


@pytest.mark.parametrize(
    ("field", "value", "part"),
    [
        ("draft_model", "claude-sonnet-5-5", "drafter.model"),
        ("draft_effort", "high", "drafter.effort"),
        ("max_steps", 3, "drafter.limits"),
        ("check_model", "claude-haiku-5-5", "checker.model"),
        ("check_effort", "low", "checker.effort"),
    ],
)
def test_a_changed_setting_names_its_part(field: str, value: Any, part: str) -> None:
    changed = dataclasses.replace(Config.current(), **{field: value})

    assert differences(drafter(), drafter(changed)) == [part]


def test_a_changed_prompt_names_its_part(monkeypatch: pytest.MonkeyPatch) -> None:
    before = drafter()
    monkeypatch.setattr("standingproof.agent.prompts.SYSTEM_PROMPT", "Answer anything.")
    assert differences(before, drafter()) == ["drafter.prompt"]

    before = drafter()
    monkeypatch.setattr("standingproof.agent.prompts.CHECKER_PROMPT", "Agree.")
    assert differences(before, drafter()) == ["checker.prompt"]


def test_a_changed_tool_description_names_its_part(monkeypatch: pytest.MonkeyPatch) -> None:
    before = drafter()
    monkeypatch.setattr("standingproof.checks.tools._description", lambda: "Look at anything.")

    assert differences(before, drafter()) == ["drafter.tools"]


def test_a_changed_output_schema_names_its_part(monkeypatch: pytest.MonkeyPatch) -> None:
    from standingproof.agent.types import AnswerDraft

    class WithConfidence(AnswerDraft):
        confidence: float = 0.0

    before = drafter()
    monkeypatch.setattr(fingerprint, "AnswerDraft", WithConfidence)

    assert differences(before, drafter()) == ["drafter.schema"]


def test_how_text_from_outside_is_wrapped_is_part_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    before = drafter()
    for module in ("prompts", "checker"):  # the checker imported the name
        monkeypatch.setattr(f"standingproof.agent.{module}.wrap", lambda tag, text, _="": text)

    assert differences(before, drafter()) == ["checker.request", "drafter.request"]


def test_a_changed_case_or_library_names_the_case() -> None:
    assert differences(drafter(), drafter(question="Something else?")) == ["case"]
    other_library = drafter(library="core")
    assert differences(drafter(), other_library) == ["case"]


def test_other_cases_and_the_order_of_keys_do_not_matter() -> None:
    fewer = DRAFTER.model_copy(update={"cases": [CASE]})

    assert of_drafter_case(fewer, CASE, "full", Config.current()) == drafter()


def test_without_the_check_tool_the_tools_differ() -> None:
    docs_only = of_drafter_case(DRAFTER, CASE, "docs_only", Config.current())

    assert differences(drafter(), docs_only) == ["drafter.tools"]


def test_a_checker_recording_does_not_depend_on_the_drafter() -> None:
    config = Config.current()
    main = of_checker_case(CHECK, "main", config)

    assert not any(part.startswith("drafter.") for part in main)
    changed = dataclasses.replace(config, draft_model="claude-sonnet-5-5")
    assert of_checker_case(CHECK, "main", changed) == main


def test_the_cheaper_reading_is_fingerprinted_with_its_own_model() -> None:
    config = Config.current()

    cheaper = of_checker_case(CHECK, "cheaper", config)

    assert differences(of_checker_case(CHECK, "main", config), cheaper) == ["checker.model"]
    assert cheaper["checker.model"] == config.cheaper_check_model


def test_a_part_missing_from_a_recording_is_a_difference() -> None:
    recorded = drafter()
    del recorded["drafter.tools"]

    assert differences(recorded, drafter()) == ["drafter.tools"]


def test_the_models_are_the_products_defaults_not_this_machines_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SP_DRAFT_MODEL", "something-else")

    assert Config.current().draft_model == "claude-opus-5-5"


def test_an_evals_settings_take_nothing_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SP_EMBEDDING_MODEL", "voyage-4-lite")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-not-a-key")
    monkeypatch.setenv("SP_SEARCH_MAX_RESULTS", "1")
    monkeypatch.setenv("SP_GITHUB_TOKEN", "github_pat_not_a_token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-key")

    made = fingerprint.settings(github_orgs="harborlight")

    assert made.embedding_model == "BAAI/bge-small-en-v1.5"
    assert (made.voyage_api_key, made.github_token, made.anthropic_api_key) == (None, None, None)
    assert (made.search_max_results, made.github_orgs) == (20, "harborlight")


def test_output_limits_and_the_embedding_model_are_parts_too() -> None:
    parts = drafter()

    assert parts["drafter.max_tokens"] == "16000"
    assert parts["checker.max_tokens"] == "8000"
    assert parts["world.embedding"] == "BAAI/bge-small-en-v1.5"


def test_only_a_redraft_case_depends_on_how_a_redraft_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.cases import load_drafter
    from standingproof.agent import prompts

    cases = load_drafter()
    plain, sent_back = cases.by_name["a-reviews-two"], cases.by_name["g-reviews-switched-off"]
    config = Config.current()
    before = {c.name: of_drafter_case(cases, c, "full", config) for c in (plain, sent_back)}

    monkeypatch.setattr(prompts, "REDRAFT_REQUEST", "Redraft it, and look again first.")

    assert of_drafter_case(cases, plain, "full", config) == before[plain.name]
    assert "drafter.redraft" not in before[plain.name]
    assert differences(
        before[sent_back.name], of_drafter_case(cases, sent_back, "full", config)
    ) == ["drafter.redraft"]

    # And on how the reviewer's words are put in the system message.
    monkeypatch.undo()
    monkeypatch.setattr(prompts, "reviewer_section", lambda words: f"The reviewer says: {words}")
    assert of_drafter_case(cases, plain, "full", config) == before[plain.name]
    assert differences(
        before[sent_back.name], of_drafter_case(cases, sent_back, "full", config)
    ) == ["drafter.redraft"]


def test_a_recording_made_before_cases_could_have_a_redraft_still_answers_for_its_case() -> None:
    import json

    from evals.cases import load_drafter
    from evals.transcript import RECORDINGS

    cases = load_drafter()
    recorded = json.loads((RECORDINGS / "drafter-full" / "a-reviews-two.1.json").read_text())

    today = of_drafter_case(cases, cases.by_name["a-reviews-two"], "full", Config.current())

    assert differences(recorded["fingerprint"], today) == []
