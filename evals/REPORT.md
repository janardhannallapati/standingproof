# Eval report

Written by `python -m evals report` from the files in `evals/recordings`. Do not edit.

Run finished 2026-11-17. 322 recordings, $2.30. Drafter `claude-opus-5-5` (medium), checker `claude-sonnet-5-5` (medium). 3 runs of each case.

## Result

| What | Count |
|---|---|
| Drafter cases right in most of 3 runs | 29 of 32 |
| Drafter cases ready and wrong in any run | 0 of 32 |
| Planted defects the checker agreed with in any run | 0 of 35 |
| Sound drafts the checker objected to in most runs | 0 of 17 |

Baseline accepted 2026-11-17: Second full run, after the checker was told which source is an observation and a failed look said not to repeat it. Four labelled drafts were added and one case's pattern was corrected. The prompts were changed after reading the first run, so this is not a first look.

## Method

A drafter case is a question and a made-up company: a library of approved answers, and what a GitHub and an AWS that exist only in the eval read. The real drafter and the real checker run on it. Code then grades the run: **right** is ready and stating what the case's world supports; **safe** is anything that went to a person (a handoff, a checker objection, or "no source" where one existed); **wrong** is ready and not supported. "Ready" means the product's rules accepted the draft and the checker agreed.

A checker case is a hand-written draft with its sources and a label: sound, or one planted defect. The checker reads each one as it would in the product.

## Drafter, by kind of case

| Kind | Runs right | Safe | Wrong |
|---|---|---|---|
| A: the live setting agrees with the library | 12 | 0 | 0 |
| B: the live setting contradicts the library | 21 | 9 | 0 |
| C: nothing in the library, and the look fails | 12 | 0 | 0 |
| D: an approved answer exists | 15 | 3 | 0 |
| E: nothing supports an answer | 12 | 0 | 0 |
| F: an instruction is planted in the question or the library | 12 | 0 | 0 |

Of the answered drafts about a live setting, 43 of 43 cite a look at it.

## With and without the live check

| Kind | With: right / safe / wrong | Library only: right / safe / wrong |
|---|---|---|
| A: the live setting agrees with the library | 12 / 0 / 0 | 4 / 0 / 0 |
| B: the live setting contradicts the library | 21 / 9 / 0 | 0 / 0 / 10 |
| C: nothing in the library, and the look fails | 12 / 0 / 0 | 4 / 0 / 0 |

"Library only" is one run of each case with the check tool taken away.

## The checker against the world

The drafter's own wording, graded by what the case's world reads, against what the checker said about it. Nobody labelled these.

| The wording was | Checker objected | Checker agreed |
|---|---|---|
| not supported by the world | 0 | 0 |
| supported by the world | 7 | 57 |

## The checker on labelled drafts

| By majority of runs | Checker objected | Checker agreed |
|---|---|---|
| 35 drafts with a planted defect | 35 | 0 |
| 17 sound drafts | 0 | 17 |

| Kind of defect | Runs in which it was caught |
|---|---|
| added_detail | 12 of 12 |
| contradicted_uncited | 15 of 15 |
| half_answered | 15 of 15 |
| planted_instruction | 12 of 12 |
| policy_vs_observed | 15 of 15 |
| stronger_word | 12 of 12 |
| wider_scope | 12 of 12 |
| wrong_number | 12 of 12 |

Agreement with the labels beyond chance (Cohen's kappa): 1.00. A finding of the expected kind: 100 of 105 catches. A quote on the planted words: 69 of 72. Quotes found in the draft: 98 of 98. Checks that did not finish: 0.

| Checker | Defects missed of 35 | False alarms of 17 |
|---|---|---|
| always object | 0 | 17 |
| always agree | 35 | 0 |
| the code rule alone | 33 | 0 |
| `claude-sonnet-5-5`, by majority | 0 | 0 |
| `claude-haiku-5-5`, one run | 0 | 2 |

## Every miss

- None.

## Whose labels

The label of each draft in the checker set was written with the draft, by its author. These are the author's labels only: no second person has labelled them.

## What these numbers cannot show

- The sets are small. Cases right: 29 of 32 (95% interval 76% to 97%). Planted defects caught by majority: 35 of 35 (95% interval 90% to 100%). Sound drafts passed: 17 of 17 (95% interval 82% to 100%).
- The cases were written by the author of the system, about one made-up company.
- A recording goes stale when a prompt, tool description, schema, model or case changes, and CI then fails. It does not notice a change in what the tools return, such as search ranking; the retrieval eval covers that separately.
