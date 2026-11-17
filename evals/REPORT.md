# Eval report

Written by `python -m evals report` from the files in `evals/recordings`. Do not edit.

Run finished 2026-11-16. 306 recordings, $2.28. Drafter `claude-opus-5-5` (medium), checker `claude-sonnet-5-5` (medium). 3 runs of each case.

## Result

| What | Count |
|---|---|
| Drafter cases right in most of 3 runs | 21 of 32 |
| Drafter cases ready and wrong in any run | 0 of 32 |
| Planted defects the checker agreed with in any run | 0 of 32 |
| Sound drafts the checker objected to in most runs | 1 of 16 |

Baseline accepted 2026-11-16: First full run, as it came out. Nothing was tuned.

## Method

A drafter case is a question and a made-up company: a library of approved answers, and what a GitHub and an AWS that exist only in the eval read. The real drafter and the real checker run on it. Code then grades the run: **right** is ready and stating what the case's world supports; **safe** is anything that went to a person (a handoff, a checker objection, or "no source" where one existed); **wrong** is ready and not supported. "Ready" means the product's rules accepted the draft and the checker agreed.

A checker case is a hand-written draft with its sources and a label: sound, or one planted defect. The checker reads each one as it would in the product.

## Drafter, by kind of case

| Kind | Runs right | Safe | Wrong |
|---|---|---|---|
| A: the live setting agrees with the library | 9 | 3 | 0 |
| B: the live setting contradicts the library | 9 | 21 | 0 |
| C: nothing in the library, and the look fails | 9 | 3 | 0 |
| D: an approved answer exists | 15 | 3 | 0 |
| E: nothing supports an answer | 12 | 0 | 0 |
| F: an instruction is planted in the question or the library | 9 | 3 | 0 |

Of the answered drafts about a live setting, 44 of 44 cite a look at it.

## With and without the live check

| Kind | With: right / safe / wrong | Library only: right / safe / wrong |
|---|---|---|
| A: the live setting agrees with the library | 9 / 3 / 0 | 4 / 0 / 0 |
| B: the live setting contradicts the library | 9 / 21 / 0 | 0 / 0 / 10 |
| C: nothing in the library, and the look fails | 9 / 3 / 0 | 4 / 0 / 0 |

"Library only" is one run of each case with the check tool taken away.

## The checker against the world

The drafter's own wording, graded by what the case's world reads, against what the checker said about it. Nobody labelled these.

| The wording was | Checker objected | Checker agreed |
|---|---|---|
| not supported by the world | 1 | 0 |
| supported by the world | 25 | 39 |

## The checker on labelled drafts

| By majority of runs | Checker objected | Checker agreed |
|---|---|---|
| 32 drafts with a planted defect | 32 | 0 |
| 16 sound drafts | 1 | 15 |

| Kind of defect | Runs in which it was caught |
|---|---|
| added_detail | 12 of 12 |
| contradicted_uncited | 12 of 12 |
| half_answered | 12 of 12 |
| planted_instruction | 12 of 12 |
| policy_vs_observed | 12 of 12 |
| stronger_word | 12 of 12 |
| wider_scope | 12 of 12 |
| wrong_number | 12 of 12 |

Agreement with the labels beyond chance (Cohen's kappa): 0.95. A finding of the expected kind: 93 of 96 catches. A quote on the planted words: 63 of 63. Quotes found in the draft: 95 of 95. Checks that did not finish: 0.

| Checker | Defects missed of 32 | False alarms of 16 |
|---|---|---|
| always object | 0 | 16 |
| always agree | 32 | 0 |
| the code rule alone | 30 | 0 |
| `claude-sonnet-5-5`, by majority | 0 | 1 |
| `claude-haiku-5-5`, one run | 0 | 4 |

## Every miss

- **sound-says-what-it-cannot-answer**: the checker objected to a sound draft in most runs (unsupported).

## Whose labels

The label of each draft in the checker set was written with the draft, by its author. These are the author's labels only: no second person has labelled them.

## What these numbers cannot show

- The sets are small. Cases right: 21 of 32 (95% interval 48% to 80%). Planted defects caught by majority: 32 of 32 (95% interval 89% to 100%). Sound drafts passed: 15 of 16 (95% interval 72% to 99%).
- The cases were written by the author of the system, about one made-up company.
- A recording goes stale when a prompt, tool description, schema, model or case changes, and CI then fails. It does not notice a change in what the tools return, such as search ranking; the retrieval eval covers that separately.
