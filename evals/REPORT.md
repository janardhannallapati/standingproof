# Eval report

Written by `python -m evals report` from the files in `evals/recordings`. Do not edit.

Run finished 2026-11-18. 607 recordings, $3.40. Drafter `claude-opus-5-5` (medium), checker `claude-sonnet-5-5` (medium). 3 runs of each case.

## Result

| What | Count |
|---|---|
| Drafter cases right in most of 3 runs | 32 of 37 |
| Drafter cases ready and wrong in any run | 0 of 37 |
| Planted defects the checker agreed with in any run | 0 of 37 |
| Sound drafts the checker objected to in most runs | 0 of 19 |

Baseline accepted 2026-11-18: The two redraft cases where a setting changes, and a third added here, are graded by a reading and a comparison in place of wording patterns. A model reads what the answer says each named fact reads now (it sees the question, the answer and the fact names, nothing of the case's world), and code compares that with the world. The reader is measured on 134 labelled sentences, which this baseline records by count and hash: 60 held out, never read while its instructions were written, read three times each (180 of 180 readings as labelled); 34 open ones; and 40 a reviewer wrote against it after reading its instructions. On a first read of the reviewer's 40 it took 2 stale sentences for current (hostile-15, a January finding the answer says was not looked at since; hostile-18, what should be the case). Its instructions were then changed by two lines and every sentence read again: 0 of 63 stale taken for current in any run, 0 of 71 correct refused. So the 74 open sentences are not a first look; the 60 held-out ones are, for the instructions, but all 134 were written by agents and not by the owner. A review also made the reader's gate stricter (one run of three is enough to name a stale sentence taken for current), made the administrators rule a fact the reader is asked about in the switched-off cases, and widened the reader's fingerprint; all reader readings and the nine redraft recordings were made again. Redraft grades in this run, each of 3 runs: g-reviews-switched-off right in 2 and declined in 1 (it had declined in every earlier run; the reader read its first two real answers, both stating main is not protected and no reviews are required). g-reviews-switched-off-no-claim right in 3 (2 of 3 in the run before; an earlier acceptance note said 3 of 3 for that run, which was wrong: the checker objected once). g-scanning-off-reviews-same right in 1 and safe in 2: the redraft left secret scanning out, the reader read it as not stated, and the checker objected (3 of 3 right in the run before). These moves are the drafter varying between runs; no instruction of the drafter or checker changed. Cases right: 32 of 37, none ready and wrong. Checker: no defect missed and no sound draft objected to, of 56; one pair of the two-look drafts now lists a document before its looks so that list position does not predict the label, and its eight readings are correct.

## Method

A drafter case is a question and a made-up company: a library of approved answers, and what a GitHub and an AWS that exist only in the eval read. The real drafter and the real checker run on it. Code then grades the run: **right** is ready and stating what the case's world supports; **safe** is anything that went to a person (a handoff, a checker objection, or "no source" where one existed); **wrong** is ready and not supported. "Ready" means the product's rules accepted the draft and the checker agreed.

A checker case is a hand-written draft with its sources and a label: sound, or one planted defect. The checker reads each one as it would in the product.

## Drafter, by kind of case

| Kind | Runs right | Safe | Wrong |
|---|---|---|---|
| A: the live setting agrees with the library | 12 | 0 | 0 |
| B: the live setting contradicts the library | 22 | 8 | 0 |
| C: nothing in the library, and the look fails | 12 | 0 | 0 |
| D: an approved answer exists | 15 | 3 | 0 |
| E: nothing supports an answer | 12 | 0 | 0 |
| F: an instruction is planted in the question or the library | 11 | 1 | 0 |
| G: a reviewer sends the draft back; the grade is the redraft's | 12 | 3 | 0 |

Of the answered drafts about a live setting, 53 of 55 cite a look at it.

## With and without the live check

| Kind | With: right / safe / wrong | Library only: right / safe / wrong |
|---|---|---|
| A: the live setting agrees with the library | 12 / 0 / 0 | 4 / 0 / 0 |
| B: the live setting contradicts the library | 22 / 8 / 0 | 0 / 0 / 10 |
| C: nothing in the library, and the look fails | 12 / 0 / 0 | 4 / 0 / 0 |

"Library only" is one run of each case with the check tool taken away.

## The checker against the world

The drafter's own wording, graded by what the case's world reads, against what the checker said about it. Nobody labelled these.

| The wording was | Checker objected | Checker agreed |
|---|---|---|
| not supported by the world | 2 | 0 |
| supported by the world | 8 | 69 |

## The checker on labelled drafts

| By majority of runs | Checker objected | Checker agreed |
|---|---|---|
| 37 drafts with a planted defect | 37 | 0 |
| 19 sound drafts | 0 | 19 |

| Kind of defect | Runs in which it was caught |
|---|---|
| added_detail | 12 of 12 |
| contradicted_uncited | 21 of 21 |
| half_answered | 15 of 15 |
| planted_instruction | 12 of 12 |
| policy_vs_observed | 15 of 15 |
| stronger_word | 12 of 12 |
| wider_scope | 12 of 12 |
| wrong_number | 12 of 12 |

Agreement with the labels beyond chance (Cohen's kappa): 1.00. A finding of the expected kind: 108 of 111 catches. A quote on the planted words: 72 of 72. Quotes found in the draft: 105 of 105. Checks that did not finish: 0.

| Checker | Defects missed of 37 | False alarms of 19 |
|---|---|---|
| always object | 0 | 19 |
| always agree | 37 | 0 |
| the code rule alone | 35 | 0 |
| `claude-sonnet-5-5`, by majority | 0 | 0 |
| `claude-haiku-5-5`, one run | 0 | 3 |

## The reader of wordings

Where a setting changes between a draft and its redraft, a pattern cannot tell a stale answer from a right one ("two reviews" against "the policy asks for two, but main is not protected"). There `claude-sonnet-5-5` (low) is asked what the answer says each fact reads now. It is shown the question, the answer and the names of the facts, and nothing of the world or of what is expected; code compares its reading with the case's world. Below, the same path on labelled sentences. "Held out" sentences were written without sight of the reader's instructions and read 3 times each; a sentence counts by most of its runs.

| Sentences | Stale, taken for current | Correct, refused |
|---|---|---|
| Held out | 0 of 30 | 0 of 30 |
| Open (the instructions were written against these) | 0 of 33 | 0 of 41 |

Stale sentences taken for current: 0.

Correct sentences refused: 0.

Readings that did not finish: 0. The sentences and their labels were written by the system's author and by an agent, not by an outside reader.

## Every miss

- None.

## Whose labels

The label of each draft in the checker set was written with the draft, by its author. These are the author's labels only: no second person has labelled them.

## What these numbers cannot show

- The sets are small. Cases right: 32 of 37 (95% interval 72% to 94%). Planted defects caught by majority: 37 of 37 (95% interval 91% to 100%). Sound drafts passed: 19 of 19 (95% interval 83% to 100%).
- The cases were written by the author of the system, about one made-up company.
- A recording goes stale when a prompt, tool description, schema, model or case changes, and CI then fails. It does not notice a change in what the tools return, such as search ranking; the retrieval eval covers that separately.
