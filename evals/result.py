"""Everything the recordings say, gathered once: grades, scores, and what has gone stale."""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from evals.cases import CheckerSet, DrafterSet
from evals.fingerprint import Config, differences, of_checker_case, of_drafter_case
from evals.graders import GradedCheck, GradedDraft, grade_check, grade_draft
from evals.plan import RUNS, Manifest, load_manifest
from evals.score import Baseline, CheckerScore, DrafterScore, gate, score_checker, score_drafter
from evals.transcript import DrafterRun, read_all

OWNER_LABELS = "owner-labels.json"


@dataclass(frozen=True)
class Result:
    drafter_set: DrafterSet
    checker_set: CheckerSet
    config: Config
    drafts: list[DrafterRun]
    graded_drafts: list[GradedDraft]
    graded_docs_only: list[GradedDraft]
    graded_checks: list[GradedCheck]
    graded_cheaper: list[GradedCheck]
    drafter: DrafterScore
    checker: CheckerScore
    cheaper: CheckerScore | None
    # Why a recording no longer answers for today's code, one line per reason.
    stale: list[str]
    manifest: Manifest | None
    cost_usd: float
    recordings: int
    owner_labels: dict[str, str] | None


def load_result(
    root: Path, drafter_set: DrafterSet, checker_set: CheckerSet, sets: Path, runs: int = RUNS
) -> Result:
    config = Config.current()
    recordings = read_all(root)
    changed: Counter[str] = Counter()
    unknown: set[str] = set()
    drafts: list[DrafterRun] = []
    graded: dict[str, list[GradedDraft]] = {"full": [], "docs_only": []}
    checks: dict[str, list[GradedCheck]] = {"main": [], "cheaper": []}
    for run in recordings:
        if isinstance(run, DrafterRun):
            case = drafter_set.by_name.get(run.case)
            if case is None:
                unknown.add(run.case)
                continue
            today = of_drafter_case(drafter_set, case, run.variant, config)
            graded[run.variant].append(grade_draft(case, run))
            if run.variant == "full":
                drafts.append(run)
        else:
            labelled = checker_set.by_name.get(run.case)
            if labelled is None:
                unknown.add(run.case)
                continue
            today = of_checker_case(labelled, run.variant, config)
            checks[run.variant].append(grade_check(labelled, run))
        changed.update(differences(run.fingerprint, today))
    stale = [
        f"{count} recordings were made with a different `{part}`: run the eval again"
        for part, count in sorted(changed.items())
    ]
    if unknown:
        stale.append(f"recordings of cases that are not in the sets: {', '.join(sorted(unknown))}")
    labels = sets / OWNER_LABELS
    return Result(
        drafter_set=drafter_set,
        checker_set=checker_set,
        config=config,
        drafts=drafts,
        graded_drafts=graded["full"],
        graded_docs_only=graded["docs_only"],
        graded_checks=checks["main"],
        graded_cheaper=checks["cheaper"],
        drafter=score_drafter(drafter_set, graded["full"], runs=runs),
        checker=score_checker(checker_set, checks["main"], runs=runs),
        cheaper=score_checker(checker_set, checks["cheaper"], runs=1)
        if checks["cheaper"]
        else None,
        stale=stale,
        manifest=load_manifest(root),
        cost_usd=sum(run.cost_usd for run in recordings),
        recordings=len(recordings),
        owner_labels=json.loads(labels.read_text()) if labels.exists() else None,
    )


def problems(result: Result, baseline: Baseline | None) -> list[str]:
    """Why this result fails the gate. Empty when it passes."""
    found = list(result.stale)
    if baseline is None:
        return [*found, "there is no baseline: run the eval and `python -m evals accept`"]
    return [*found, *gate(baseline, result.drafter, result.checker)]


def not_whole(result: Result) -> list[str]:
    """Why this result may not become the baseline. Empty when it may."""
    found = list(result.stale)
    if result.manifest is None or not result.manifest.complete:
        found.append("the last run did not finish, so its numbers are not a result")
    missing = [*result.drafter.missing, *result.checker.missing]
    if missing:
        found.append(f"no recording, or too few, for: {', '.join(missing)}")
    return found
