"""REPORT.md, written from the recordings alone. Every number is a count out of a total."""

from collections import Counter

from evals.cases import CheckerSet
from evals.graders import GradedDraft
from evals.result import Result
from evals.score import Baseline, kappa, wilson

_GRADES = ("right", "safe", "wrong")


def _of(count: int, total: int) -> str:
    return f"{count} of {total}"


def _between(count: int, total: int) -> str:
    low, high = wilson(count, total)
    return f"{_of(count, total)} (95% interval {low:.0%} to {high:.0%})"


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return [*lines, ""]


def _families(graded: list[GradedDraft], result: Result, families: str) -> dict[str, Counter[str]]:
    family_of = {case.name: case.family for case in result.drafter_set.cases}
    counts: dict[str, Counter[str]] = {family: Counter() for family in families}
    for item in graded:
        if family_of[item.case] in counts:
            counts[family_of[item.case]][item.grade] += 1
    return counts


_FAMILY = {
    "A": "the live setting agrees with the library",
    "B": "the live setting contradicts the library",
    "C": "nothing in the library, and the look fails",
    "D": "an approved answer exists",
    "E": "nothing supports an answer",
    "F": "an instruction is planted in the question or the library",
    "G": "a reviewer sends the draft back; the grade is the redraft's",
}


def _misses(result: Result) -> list[str]:
    lines: list[str] = []
    drafts = {(run.case, run.run): run for run in result.drafts}
    for item in result.graded_drafts:
        if item.grade != "wrong":
            continue
        draft = drafts[(item.case, item.run)].final_draft
        lines += [
            f"- **{item.case}**, run {item.run}: ready and wrong ({'; '.join(item.reasons)}).",
            f'  Wording: "{draft.answer if draft else ""}"',
        ]
    by_case = result.checker_set.by_name
    for check in result.graded_checks:
        if by_case[check.case].label == "not_supported" and not check.objected:
            lines.append(
                f"- **{check.case}**, run {check.run}: the checker agreed with a planted "
                f"defect ({by_case[check.case].defect})."
            )
    for name in result.checker.false_alarm_cases:
        kinds = sorted({kind for g in result.graded_checks if g.case == name for kind in g.kinds})
        lines.append(
            f"- **{name}**: the checker objected to a sound draft in most runs "
            f"({', '.join(kinds) or 'no finding'})."
        )
    return lines or ["- None."]


def _labels(result: Result) -> list[str]:
    if result.owner_labels is None:
        return [
            "The label of each draft in the checker set was written with the draft, by its "
            "author. These are the author's labels only: no second person has labelled them.",
            "",
        ]
    cases: CheckerSet = result.checker_set
    shared = [case for case in cases.cases if case.name in result.owner_labels]
    agree = sum(result.owner_labels[case.name] == case.label for case in shared)
    k = kappa(
        [case.label == "not_supported" for case in shared],
        [result.owner_labels[case.name] == "not_supported" for case in shared],
    )
    return [
        f"A second person labelled {len(shared)} of the drafts without seeing the labels. "
        f"The two agree on {_of(agree, len(shared))} (Cohen's kappa {k:.2f}).",
        "",
    ]


def render(result: Result, baseline: Baseline | None) -> str:
    d, c, config = result.drafter, result.checker, result.config
    defects = sum(case.label == "not_supported" for case in result.checker_set.cases)
    sound = c.cases - defects
    finished = result.manifest.finished_at[:10] if result.manifest else "unknown"
    out = [
        "# Eval report",
        "",
        "Written by `python -m evals report` from the files in `evals/recordings`. Do not edit.",
        "",
        f"Run finished {finished}. {result.recordings} recordings, ${result.cost_usd:.2f}. "
        f"Drafter `{config.draft_model}` ({config.draft_effort}), checker "
        f"`{config.check_model}` ({config.check_effort}). {d.runs} runs of each case.",
        "",
        "## Result",
        "",
    ]
    out += _table(
        ["What", "Count"],
        [
            [f"Drafter cases right in most of {d.runs} runs", _of(d.right_cases, d.cases)],
            ["Drafter cases ready and wrong in any run", _of(len(d.wrong_cases), d.cases)],
            [
                "Planted defects the checker agreed with in any run",
                _of(len(c.missed_cases), defects),
            ],
            [
                "Sound drafts the checker objected to in most runs",
                _of(len(c.false_alarm_cases), sound),
            ],
        ],
    )
    if baseline is not None:
        out += [f"Baseline accepted {baseline.accepted_on}: {baseline.note}", ""]
    if result.stale:
        out += ["**These recordings do not match today's code:**", ""]
        out += [f"- {line}" for line in result.stale] + [""]
    out += [
        "## Method",
        "",
        "A drafter case is a question and a made-up company: a library of approved answers, "
        "and what a GitHub and an AWS that exist only in the eval read. The real drafter and "
        "the real checker run on it. Code then grades the run: **right** is ready and stating "
        "what the case's world supports; **safe** is anything that went to a person (a "
        'handoff, a checker objection, or "no source" where one existed); **wrong** is ready '
        'and not supported. "Ready" means the product\'s rules accepted the draft and the '
        "checker agreed.",
        "",
        "A checker case is a hand-written draft with its sources and a label: sound, or one "
        "planted defect. The checker reads each one as it would in the product.",
        "",
        "## Drafter, by kind of case",
        "",
    ]
    families = _families(result.graded_drafts, result, "ABCDEFG")
    out += _table(
        ["Kind", "Runs right", "Safe", "Wrong"],
        [[f"{f}: {_FAMILY[f]}", *(families[f][g] for g in _GRADES)] for f in "ABCDEFG"],
    )
    out += [
        f"Of the answered drafts about a live setting, {_of(*d.looked)} cite a look at it.",
        "",
    ]
    if result.graded_docs_only:
        without = _families(result.graded_docs_only, result, "ABC")
        out += ["## With and without the live check", ""]
        out += _table(
            ["Kind", "With: right / safe / wrong", "Library only: right / safe / wrong"],
            [
                [
                    f"{f}: {_FAMILY[f]}",
                    " / ".join(str(families[f][g]) for g in _GRADES),
                    " / ".join(str(without[f][g]) for g in _GRADES),
                ]
                for f in "ABC"
            ],
        )
        out += ['"Library only" is one run of each case with the check tool taken away.', ""]
    j = d.judge
    out += ["## The checker against the world", ""]
    out += [
        "The drafter's own wording, graded by what the case's world reads, against what the "
        "checker said about it. Nobody labelled these.",
        "",
    ]
    out += _table(
        ["The wording was", "Checker objected", "Checker agreed"],
        [
            ["not supported by the world", j["wrong_objected"], j["wrong_agreed"]],
            ["supported by the world", j["sound_objected"], j["sound_agreed"]],
        ],
    )
    out += ["## The checker on labelled drafts", ""]
    m = c.matrix
    out += _table(
        ["By majority of runs", "Checker objected", "Checker agreed"],
        [
            [f"{defects} drafts with a planted defect", m["caught"], m["missed"]],
            [f"{sound} sound drafts", m["false_alarm"], m["passed"]],
        ],
    )
    out += _table(
        ["Kind of defect", "Runs in which it was caught"],
        [[name, _of(*count)] for name, count in sorted(c.by_defect.items())],
    )
    out += [
        f"Agreement with the labels beyond chance (Cohen's kappa): {c.kappa:.2f}. "
        f"A finding of the expected kind: {_of(*c.kind_ok)} catches. A quote on the planted "
        f"words: {_of(*c.phrase_hit)}. Quotes found in the draft: {_of(*c.located)}. "
        f"Checks that did not finish: {c.not_checked_runs}.",
        "",
    ]
    rows = [[name, row["missed"], row["false_alarms"]] for name, row in c.baselines.items()]
    rows.append([f"`{config.check_model}`, by majority", m["missed"], m["false_alarm"]])
    if result.cheaper is not None:
        cm = result.cheaper.matrix
        rows.append([f"`{config.cheaper_check_model}`, one run", cm["missed"], cm["false_alarm"]])
    out += _table(["Checker", f"Defects missed of {defects}", f"False alarms of {sound}"], rows)
    out += ["## Every miss", "", *_misses(result), ""]
    out += ["## Whose labels", "", *_labels(result)]
    out += [
        "## What these numbers cannot show",
        "",
        f"- The sets are small. Cases right: {_between(d.right_cases, d.cases)}. Planted "
        f"defects caught by majority: {_between(m['caught'], defects)}. Sound drafts passed: "
        f"{_between(m['passed'], sound)}.",
        "- The cases were written by the author of the system, about one made-up company.",
        "- A recording goes stale when a prompt, tool description, schema, model or case "
        "changes, and CI then fails. It does not notice a change in what the tools return, "
        "such as search ranking; the retrieval eval covers that separately.",
        "",
    ]
    return "\n".join(out)
