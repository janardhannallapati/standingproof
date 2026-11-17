"""The commands: score (the CI gate), accept, report, and run's refusal without a yes."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals import __main__ as cli
from evals.cases import CheckerSet, DrafterSet, load_checker, load_drafter
from evals.fingerprint import Config, of_checker_case, of_drafter_case
from evals.plan import MANIFEST
from evals.transcript import holds_secret, looks_secret, read_all, write
from tests.eval.runs import (
    AGREES,
    EVIDENCE,
    LIBRARY,
    OBJECTS,
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
        alone = drafted(D.cases[0].name, TWO, [LIBRARY], check=OBJECTS, variant="docs_only")
        today = of_drafter_case(D, D.cases[0], "docs_only", CONFIG)
        write(alone.model_copy(update={"fingerprint": today}), self.root)
        for labelled in C.cases:
            today = of_checker_case(labelled, "cheaper", CONFIG)
            write(checked(labelled.name, AGREES, variant="cheaper", fingerprint=today), self.root)
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
        "claude-opus-5-5" in text and "2026-11-16" in text and "$0.16" in text
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
        assert not holds_secret(json.loads(path.read_text())), path.name


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


def test_a_recording_in_the_wrong_place_fails_score(repo: Repo) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    source = repo.root / "checker-main" / "sound-backups.1.json"
    (repo.root / "checker-main" / "sound-backups.9.json").write_text(source.read_text())

    assert repo.run("score") == 1
    assert "sound-backups.9.json" in repo.said


@pytest.mark.parametrize("where", ["stray.json", "drafter-full/deeper/x.1.json"])
def test_a_file_the_grader_would_not_read_fails_score(repo: Repo, where: str) -> None:
    repo.record()
    repo.run("accept", "--note", "first")
    stray = repo.root / where
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}")

    assert repo.run("score") == 1
    assert Path(where).name in repo.said


def test_accept_needs_the_comparison_runs_too(repo: Repo) -> None:
    repo.record()
    for path in [*repo.root.glob("drafter-docs_only/*"), *repo.root.glob("checker-cheaper/*")]:
        path.unlink()

    assert repo.run("accept", "--note", "x") == 1
    assert "b-reviews-two-vs-one" in repo.said and "sound-backups" in repo.said


def test_a_recording_that_holds_something_shaped_like_a_key_is_not_written(tmp_path: Path) -> None:
    run = drafted("d-backups", "Our key is AKIAIOSFODNN7EXAMPLE.", [LIBRARY])

    with pytest.raises(ValueError, match="shaped like a credential"):
        write(run, tmp_path)
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize(
    "text",
    [
        # A made-up temporary key id: the example key from AWS's documentation with the
        # other prefix. Joined here so that a scanner of this repository does not match it.
        "ASIA" + "IOSFODNN7EXAMPLE",
        "postgresql+asyncpg://app:hunter2@db:5432/x",
        "pa-abcdefghijklmnopqrstuv",
    ],
)
def test_more_shapes_of_credential_are_recognised(text: str) -> None:
    assert looks_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN DSA PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_every_kind_of_private_key_header_is_recognised(text: str) -> None:
    assert looks_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "see https://vault.internal:8200\nsecurity@harborlight.example",
        "policy-pa-ssword-rotation-policy-2024.md",
        "THEQUICKBROWNFOXAKIAJUMPSOVERTHELAZYDOGAGAIN",
        "https://example.com/a:b@c",
        "git@github.com:org/repo",
    ],
)
def test_ordinary_text_is_not_taken_for_a_credential(text: str) -> None:
    assert not looks_secret(text)


@pytest.mark.parametrize(
    "note",
    [
        "the key is:\nAKIAIOSFODNN7EXAMPLE",
        "the key is:\tAKIAIOSFODNN7EXAMPLE",
        "the key is \u2014AKIAIOSFODNN7EXAMPLE",
        "AWS_KEY_AKIAIOSFODNN7EXAMPLE",
        "voyage:\npa-abcd-efghijklmnopqrstuvwx",
        "redis://:hunter2@cache:6379/0",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    ],
)
def test_a_key_is_found_wherever_it_sits_in_a_recording(note: str, tmp_path: Path) -> None:
    run = drafted("d-backups", "Daily.", [LIBRARY])
    assert run.final_draft is not None
    leaked = run.model_copy(
        update={"final_draft": run.final_draft.model_copy(update={"note": note})}
    )

    with pytest.raises(ValueError, match="shaped like a credential"):
        write(leaked, tmp_path)


def test_the_client_does_not_retry_behind_the_meters_back() -> None:
    assert cli.provider_client("not-a-key").max_retries == 0


def test_accept_refuses_a_result_with_a_check_that_did_not_finish(repo: Repo) -> None:
    repo.record()
    unread = repo.root / "checker-main" / "wider-contractors.2.json"
    data = json.loads(unread.read_text())
    data["turn"] = None
    unread.write_text(json.dumps(data))

    assert repo.run("accept", "--note", "x") == 1
    assert "did not finish" in repo.said and not repo.baseline.exists()


def test_a_comparison_recording_must_be_run_one(repo: Repo) -> None:
    repo.record()
    one = repo.root / "checker-cheaper" / "sound-backups.1.json"
    data = json.loads(one.read_text()) | {"run": 2}
    one.unlink()
    (one.parent / "sound-backups.2.json").write_text(json.dumps(data))

    assert repo.run("accept", "--note", "x") == 1


def test_nothing_else_the_eval_commits_holds_anything_shaped_like_a_key() -> None:
    from evals.transcript import RECORDINGS

    for path in (RECORDINGS / MANIFEST, cli.REPORT, cli.BASELINE):
        if path.exists():
            assert not looks_secret(path.read_text()), path.name


@pytest.mark.parametrize("which", ["drafter-docs_only", "checker-cheaper"])
def test_accept_refuses_a_comparison_whose_check_did_not_finish(repo: Repo, which: str) -> None:
    repo.record()
    [path] = list((repo.root / which).glob("*backups.1.json")) or list(
        (repo.root / which).glob("*.1.json")
    )
    data = json.loads(path.read_text())
    data["turn" if which.startswith("checker") else "check_turn"] = None
    path.write_text(json.dumps(data))

    assert repo.run("accept", "--note", "x") == 1
    assert "did not finish" in repo.said
