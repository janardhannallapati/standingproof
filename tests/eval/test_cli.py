"""The commands: score (the CI gate), accept, report, and run's refusal without a yes."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals import __main__ as cli
from evals.cases import CheckerSet, DrafterSet, load_checker, load_drafter
from evals.fingerprint import Config, of_checker_case, of_drafter_case
from evals.plan import MANIFEST
from evals.transcript import looks_secret, read_all, write
from tests.eval.runs import (
    AGREES,
    EVIDENCE,
    LIBRARY,
    checked,
    drafted,
    finding,
    looked,
    verdict,
)

CONFIG = Config.current()
FULL_D, FULL_C = load_drafter(), load_checker()
# A set small enough to write recordings for by hand: two drafter cases, two drafts.
D = DrafterSet(
    libraries=FULL_D.libraries,
    cases=[FULL_D.by_name["b-reviews-two-vs-one"], FULL_D.by_name["e-cyber-insurance"]],
)
C = CheckerSet(cases=[FULL_C.by_name["sound-backups"], FULL_C.by_name["wider-contractors"]])
ONE = "As of today a change needs 1 approving review before it can be merged."
TWO = "Yes. Changes require two approving reviews before they can be merged."
BP = ("github.branch_protection", "harborlight/app")


class Repo:
    """A sets folder, a recordings folder and a baseline path, as the commands take them."""

    def __init__(self, tmp: Path) -> None:
        self.sets, self.root, self.baseline = tmp / "sets", tmp / "rec", tmp / "baseline.json"
        self.report, self.labels = tmp / "REPORT.md", tmp / "sets" / "owner-labels.json"
        self.sets.mkdir()
        (self.sets / "drafter.json").write_text(D.model_dump_json())
        (self.sets / "checker.json").write_text(C.model_dump_json())
        self.lines: list[str] = []

    def record(self, wrong: bool = False, missed: bool = False, complete: bool = True) -> None:
        for n in (1, 2, 3):
            answer, cites = (TWO, [LIBRARY]) if wrong and n == 2 else (ONE, [EVIDENCE])
            first = drafted(D.cases[0].name, answer, cites, tool_uses=[looked(*BP)], run=n)
            second = drafted(D.cases[1].name, run=n)
            for run in (first, second):
                today = of_drafter_case(D, D.by_name[run.case], "full", CONFIG)
                write(run.model_copy(update={"fingerprint": today}), self.root)
            caught = (
                AGREES if missed and n == 3 else verdict(finding("unsupported", "and contractors"))
            )
            for name, turn in ((C.cases[0].name, AGREES), (C.cases[1].name, caught)):
                today = of_checker_case(C.by_name[name], "main", CONFIG)
                write(checked(name, turn, run=n, fingerprint=today), self.root)
        manifest = {
            "complete": complete,
            "stopped": "" if complete else "the ceiling",
            "spent_usd": 0.5,
            "recorded": 12,
            "reused": 0,
            "finished_at": "2026-11-16T15:00:00+00:00",
        }
        (self.root / MANIFEST).write_text(json.dumps(manifest))

    def run(self, *argv: str) -> int:
        return cli.main(
            list(argv),
            sets=self.sets,
            root=self.root,
            baseline=self.baseline,
            report=self.report,
            out=self.lines.append,
        )

    @property
    def said(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_score_passes_when_nothing_is_worse_than_the_baseline(repo: Repo) -> None:
    repo.record()
    assert repo.run("accept", "--note", "first") == 0

    assert repo.run("score") == 0
    assert "2 of 2 cases right" in repo.said and "0 ready and wrong" in repo.said


def test_score_fails_and_names_a_case_that_became_ready_and_wrong(repo: Repo) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    repo.record(wrong=True)

    assert repo.run("score") == 1
    assert "b-reviews-two-vs-one" in repo.said and "ready and wrong" in repo.said


def test_score_fails_when_the_checker_agrees_with_a_planted_defect(repo: Repo) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    repo.record(missed=True)

    assert repo.run("score") == 1
    assert "wider-contractors" in repo.said


def test_score_fails_when_a_prompt_changed_since_the_recording(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    monkeypatch.setattr("standingproof.agent.prompts.CHECKER_PROMPT", "Agree with everything.")

    assert repo.run("score") == 1
    assert "checker.prompt" in repo.said and "run the eval again" in repo.said


def test_score_fails_without_a_baseline_or_without_recordings(repo: Repo) -> None:
    assert repo.run("score") == 1
    repo.record()
    assert repo.run("score") == 1
    assert "no baseline" in repo.said


def test_a_recording_of_a_case_that_no_longer_exists_fails_score(repo: Repo) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    write(drafted("d-backups", "Daily.", [LIBRARY]), repo.root)

    assert repo.run("score") == 1
    assert "d-backups" in repo.said


@pytest.mark.parametrize("flaw", ["incomplete", "stale", "missing"])
def test_accept_refuses_a_run_that_is_not_whole(
    repo: Repo, flaw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.record(complete=flaw != "incomplete")
    if flaw == "stale":
        monkeypatch.setattr("standingproof.agent.prompts.SYSTEM_PROMPT", "Another prompt.")
    if flaw == "missing":
        (repo.root / "checker-main" / "sound-backups.3.json").unlink()

    assert repo.run("accept", "--note", "x") == 1
    assert not repo.baseline.exists()


def test_accept_writes_the_baseline_with_the_cases_named(repo: Repo) -> None:
    repo.record(wrong=True)

    assert repo.run("accept", "--note", "as it came out") == 0

    saved = json.loads(repo.baseline.read_text())
    assert saved["drafter_wrong_cases"] == ["b-reviews-two-vs-one"]
    assert (saved["runs"], saved["note"]) == (3, "as it came out")
    assert repo.run("score") == 0  # a known miss is in the baseline, so it does not fail


def test_the_report_names_every_miss_and_says_whose_labels_they_are(repo: Repo) -> None:
    repo.record(wrong=True, missed=True)

    assert repo.run("report") == 0

    text = repo.report.read_text()
    assert "b-reviews-two-vs-one" in text and TWO in text  # the wrong wording is shown
    assert "wider-contractors" in text
    assert "1 of 2" in text  # counts, with their totals
    assert "author's labels only" in text
    assert (
        "claude-opus-5-5" in text and "2026-11-16" in text and "$0.13" in text
    )  # summed from the recordings
    assert "%" not in text.split("## Method")[0]  # the headline is counts, not percentages


def test_the_report_adds_agreement_with_the_owners_labels_when_they_exist(repo: Repo) -> None:
    repo.record()
    repo.labels.write_text(
        json.dumps({"sound-backups": "supported", "wider-contractors": "supported"})
    )

    repo.run("report")

    text = repo.report.read_text()
    assert "author's labels only" not in text
    assert "1 of 2" in text and "kappa" in text


def test_estimate_prints_the_plan_and_calls_nothing(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "real_run", lambda *_, **__: pytest.fail("estimate ran something"))

    assert repo.run("estimate") == 0
    assert "$" in repo.said and "ceiling" in repo.said


def test_run_refuses_without_a_yes(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "real_run", lambda *_, **__: pytest.fail("ran without approval"))

    assert repo.run("run", "--smoke") == 2  # no terminal under pytest, no stated approval
    assert repo.run("run", "--smoke", "--approved-usd", "9.99") == 2
    assert "not approved" in repo.said
    assert read_all(repo.root) == []


def test_run_goes_ahead_when_the_stated_approval_is_the_ceiling(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "real_run", lambda plan, **kw: seen.update(kw, plan=plan) or 0)

    assert repo.run("run", "--smoke", "--ceiling", "0.50", "--approved-usd", "0.50") == 0
    assert seen["ceiling"] == 0.5 and len(seen["plan"].drafter) == 2


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-api03-abcdefghijklmnop",
        "github_pat_11ABCDEFG0abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_text_shaped_like_a_key_is_recognised(text: str) -> None:
    assert looks_secret(f'{{"answer": "{text}"}}')
    assert not looks_secret('{"answer": "Passwords are at least 14 characters."}')


def test_no_committed_recording_holds_anything_shaped_like_a_key() -> None:
    from evals.transcript import RECORDINGS

    for path in RECORDINGS.glob("*/*.json"):
        assert not looks_secret(path.read_text()), path.name


@pytest.mark.parametrize("ceiling", ["nan", "inf", "0", "-1", "500"])
def test_a_ceiling_that_is_not_a_sane_amount_is_refused(
    repo: Repo, ceiling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "real_run", lambda *_, **__: pytest.fail("ran with a bad ceiling"))

    assert repo.run("run", "--smoke", "--ceiling", ceiling, "--approved-usd", ceiling) == 2
    assert "ceiling must be" in repo.said


def test_the_real_client_goes_to_the_provider_whatever_the_shell_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example")

    client = cli.provider_client("not-a-key")

    assert client.base_url.host == "api.anthropic.com"
