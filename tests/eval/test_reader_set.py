"""The reader's own labelled sentences: the set, its recordings, the plan and the score."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import ReaderSet, load_checker, load_drafter, load_reader
from evals.fingerprint import Config, differences, of_drafter_case, of_sentence
from evals.graders import grade_reading
from evals.plan import estimate, plan_for
from evals.reader import ReadTurn
from evals.score import (
    Baseline,
    as_baseline,
    gate,
    score_checker,
    score_drafter,
    score_reader,
)
from evals.transcript import ReaderRun, read_all, write
from tests.eval.readings import CUT, NOW_OFF, OFF, STILL_TWO, TWO, read, sentence

DRAFTER, CHECKER = load_drafter(), load_checker()
CONFIG = Config.current()


# The set


def test_the_committed_sentences_load_and_each_names_a_case_that_asks_for_a_reading() -> None:
    sentences = load_reader()

    assert {s.case for s in sentences.sentences} == {OFF, TWO}
    assert {s.part for s in sentences.sentences} >= {"open"}
    for s in sentences.sentences:
        assert DRAFTER.by_name[s.case].expect.states, s.name
    # Both labels, for both cases: a reader that always says one thing cannot pass.
    for part in {s.part for s in sentences.sentences}:
        for name in (OFF, TWO):
            mine = [s.label for s in sentences.sentences if (s.part, s.case) == (part, name)]
            assert mine.count("states_now") >= 5 and mine.count("does_not") >= 5, (part, name)


def test_two_sentences_cannot_share_a_name_or_a_wording() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        ReaderSet(sentences=[sentence(), sentence()])
    with pytest.raises(ValidationError, match="same wording"):
        ReaderSet(sentences=[sentence(), sentence(name="s-2", label="does_not")])


def test_a_sentence_for_a_case_that_asks_for_no_reading_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "reader.json"
    path.write_text(
        json.dumps({"sentences": [sentence().model_dump() | {"case": "a-reviews-two"}]})
    )

    with pytest.raises(ValueError, match="asks for no reading"):
        load_reader(path, DRAFTER)


# The fingerprint


def test_a_sentence_recording_depends_on_the_reader_and_the_sentence_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals import reader as module

    s = sentence()
    before = of_sentence(s, DRAFTER.by_name[OFF], CONFIG)

    assert set(before) == {
        "reader.prompt",
        "reader.schema",
        "reader.request",
        "reader.model",
        "reader.effort",
        "reader.max_tokens",
        "case",
    }
    other = s.model_copy(update={"answer": "Two reviews."})
    assert differences(before, of_sentence(other, DRAFTER.by_name[OFF], CONFIG)) == ["case"]
    cheaper = dataclasses.replace(CONFIG, read_model="claude-haiku-5-5")
    assert differences(before, of_sentence(s, DRAFTER.by_name[OFF], cheaper)) == ["reader.model"]
    monkeypatch.setattr(module, "READER_PROMPT", "Read it.")
    assert differences(before, of_sentence(s, DRAFTER.by_name[OFF], CONFIG)) == ["reader.prompt"]


def test_a_reading_depends_on_how_its_own_facts_are_described_and_how_its_text_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    from evals import cases
    from evals import reader as module

    off, two = DRAFTER.by_name[OFF], DRAFTER.by_name[TWO]
    s = sentence()

    def parts() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        return (
            of_sentence(s, two, CONFIG),
            of_drafter_case(DRAFTER, two, "full", CONFIG),
            of_sentence(s, off, CONFIG),
        )

    before = parts()
    # A fact only the second case asks about is described differently.
    scanning = {"enabled": "whether it is on. If unsure say false"}
    monkeypatch.setitem(cases.READABLE, "github.secret_scanning", scanning)
    after = parts()
    assert differences(before[0], after[0]) == ["reader.request"]
    assert differences(before[1], after[1]) == ["reader.request"]
    assert differences(before[2], after[2]) == []
    monkeypatch.undo()

    # The answer's text is no longer kept from closing its tag.
    monkeypatch.setattr(module, "_FACTS_TAG", re.compile(r"(?!)"))
    assert differences(before[2], parts()[2]) == ["reader.request"]


def test_only_a_case_whose_wording_is_read_depends_on_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals import reader as module

    plain, read_case = DRAFTER.by_name["a-reviews-two"], DRAFTER.by_name[OFF]
    before = {c.name: of_drafter_case(DRAFTER, c, "full", CONFIG) for c in (plain, read_case)}
    assert not any(part.startswith("reader.") for part in before[plain.name])

    monkeypatch.setattr(module, "READER_PROMPT", "Read it.")

    assert of_drafter_case(DRAFTER, plain, "full", CONFIG) == before[plain.name]
    assert differences(
        before[read_case.name], of_drafter_case(DRAFTER, read_case, "full", CONFIG)
    ) == ["reader.prompt"]


# The plan and the score


def test_the_full_plan_reads_held_out_sentences_three_times_and_open_ones_once() -> None:
    sentences = ReaderSet(
        sentences=[
            sentence(),
            sentence("does_not", "open", "s-2").model_copy(update={"answer": "Two."}),
        ]
    )

    plan = plan_for(DRAFTER, CHECKER, smoke=False, reader=sentences)

    assert plan.reader == [("s-1", 1), ("s-2", 1), ("s-1", 2), ("s-1", 3)]
    assert estimate(plan).lines[-1].calls == 4
    only = plan_for(DRAFTER, CHECKER, smoke=False, reader=sentences, only_reader=True)
    assert (only.drafter, only.docs_only, only.checker, only.cheaper) == ([], [], [], [])
    assert len(only.reader) == 4
    assert [line.calls for line in estimate(only).lines] == [0, 0, 0, 0, 0, 4]


def _sentences() -> ReaderSet:
    made = []
    for n in range(4):
        label = "states_now" if n % 2 == 0 else "does_not"
        made.append(sentence(label, "held_out", f"h-{n}").model_copy(update={"answer": f"W{n}."}))
    return ReaderSet(sentences=made)


def test_a_stale_sentence_passed_once_is_named_and_a_correct_one_is_refused_by_majority() -> None:
    # The two directions are not alike. A stale sentence taken for current lets a ready and
    # wrong answer be graded right, so one run of three is enough to name it, as one run is
    # for a defect the checker misses. A correct sentence refused is the noisy direction.
    sentences, case = _sentences(), DRAFTER.by_name[OFF]
    replies = {
        "h-0": [NOW_OFF, NOW_OFF, STILL_TWO],  # correct, read as current in most runs
        "h-1": [STILL_TWO, STILL_TWO, NOW_OFF],  # stale, read as current in one run
        "h-2": [STILL_TWO, STILL_TWO, NOW_OFF],  # correct, refused in most runs
        "h-3": [STILL_TWO, STILL_TWO, STILL_TWO],  # stale, refused every time
    }
    graded = [
        grade_reading(s, case, read(s, reply, run))
        for s in sentences.sentences
        for run, reply in enumerate(replies[s.name], start=1)
    ]

    score = score_reader(sentences, graded, runs=3)

    assert (score.stale_passed, score.correct_refused) == (["h-1"], ["h-2"])
    assert score.by_part["held_out"] == {"stale": (1, 2), "correct": (1, 2)}
    assert (score.not_read_runs, score.missing) == (0, [])


def test_the_gate_fails_when_the_labelled_sentences_are_not_the_ones_accepted() -> None:
    # Without this a hard sentence could be added, fail, and be taken out again, or the
    # whole set removed, and the gate would pass with cases still graded by the reader.
    sentences, case = _sentences(), DRAFTER.by_name[OFF]
    good = {"h-0": NOW_OFF, "h-1": STILL_TWO, "h-2": NOW_OFF, "h-3": STILL_TWO}

    def scored(kept: ReaderSet) -> Any:
        graded = [
            grade_reading(s, case, read(s, good[s.name], run))
            for s in kept.sentences
            for run in (1, 2, 3)
        ]
        return score_reader(kept, graded, runs=3)

    drafter, checker = _scores()
    baseline = as_baseline(drafter, checker, on="2026-11-18", note="n", reader=scored(sentences))
    assert baseline.reader_sentences == 4 and baseline.reader_set

    def said(reader: Any) -> list[str]:
        return [p for p in gate(baseline, drafter, checker, reader) if "labelled sentences" in p]

    assert said(scored(sentences)) == []
    assert said(scored(ReaderSet(sentences=sentences.sentences[:3])))
    assert said(None)
    reworded = sentences.sentences[0].model_copy(update={"answer": "Other words."})
    assert said(scored(ReaderSet(sentences=[reworded, *sentences.sentences[1:]])))


def test_a_baseline_from_before_the_sentences_were_recorded_in_it_asks_nothing_of_them() -> None:
    old = Baseline.model_validate(
        {
            "runs": 3,
            "drafter_wrong_cases": [],
            "drafter_right_cases": 0,
            "checker_missed_cases": [],
            "checker_false_alarm_cases": 0,
            "accepted_on": "2026-11-17",
            "note": "before there was a reader",
        }
    )

    assert (old.reader_sentences, old.reader_set, old.reader_stale_passed) == (0, "", [])


def _scores() -> tuple[Any, Any]:
    return score_drafter(DRAFTER, [], runs=3), score_checker(CHECKER, [], runs=3)


def test_the_gate_names_a_stale_sentence_the_reader_newly_passes() -> None:
    sentences, case = _sentences(), DRAFTER.by_name[OFF]
    good = {"h-0": NOW_OFF, "h-1": STILL_TWO, "h-2": NOW_OFF, "h-3": STILL_TWO}

    def scored(replies: dict[str, ReadTurn]) -> Any:
        graded = [
            grade_reading(s, case, read(s, replies[s.name], run))
            for s in sentences.sentences
            for run in (1, 2, 3)
        ]
        return score_reader(sentences, graded, runs=3)

    baseline = Baseline(
        runs=3,
        drafter_wrong_cases=[],
        drafter_right_cases=0,
        checker_missed_cases=[c.name for c in CHECKER.cases if c.label == "not_supported"],
        checker_false_alarm_cases=len(CHECKER.cases),
        reader_sentences=4,
        reader_set=scored(good).set_hash,
        accepted_on="2026-11-18",
        note="made up",
    )
    drafter, checker = _scores()
    quiet = [p for p in gate(baseline, drafter, checker, scored(good)) if "reader" in p]
    assert quiet == []

    worse = gate(baseline, drafter, checker, scored(good | {"h-1": NOW_OFF}))
    assert "the reader took a stale sentence for a current one: h-1" in worse
    refused = gate(baseline, drafter, checker, scored(good | {"h-0": STILL_TWO, "h-2": STILL_TWO}))
    assert any("2 correct sentences" in problem for problem in refused)
    unread = gate(baseline, drafter, checker, scored(good | {"h-3": CUT}))
    assert any("did not finish" in problem and "reader" in problem for problem in unread)


# Recordings


def test_a_reading_of_a_sentence_is_a_recording_in_its_own_place(tmp_path: Path) -> None:
    s = sentence()
    path = write(read(s, NOW_OFF), tmp_path)

    assert path == tmp_path / "reader-main" / "s-1.1.json"
    [back] = read_all(tmp_path)
    assert isinstance(back, ReaderRun) and back.turn == NOW_OFF


def test_a_recording_made_before_answers_were_read_still_loads() -> None:
    from evals.transcript import RECORDINGS, DrafterRun

    old = json.loads((RECORDINGS / "drafter-full" / "a-reviews-two.1.json").read_text())

    assert "reading" not in old
    assert DrafterRun.model_validate(old).reading is None


# The commands


def test_only_the_readers_sentences_can_be_estimated_and_not_with_another_short_run(
    tmp_path: Path,
) -> None:
    from evals.__main__ import NOT_APPROVED, main

    said: list[str] = []
    assert main(["estimate", "--only-reader"], root=tmp_path, out=said.append) == 0
    counts = [int(line.split(" x ")[0]) for line in said[:-1]]
    assert counts[:5] == [0, 0, 0, 0, 0] and counts[5] > 0
    assert "labelled sentences read by the reader" in said[5]

    refused: list[str] = []
    words = ["estimate", "--only-reader", "--smoke"]
    assert main(words, root=tmp_path, out=refused.append) == NOT_APPROVED
    assert "--only-reader" in refused[0]


def test_the_report_counts_the_reader_both_ways_and_quotes_what_it_got_wrong(
    tmp_path: Path,
) -> None:
    from evals.cases import SETS
    from evals.report import render
    from evals.result import load_result, not_whole

    sentences = _sentences()
    replies = {"h-0": NOW_OFF, "h-1": NOW_OFF, "h-2": NOW_OFF, "h-3": STILL_TWO}
    for s in sentences.sentences:
        for run in (1, 2, 3):
            write(read(s, replies[s.name], run), tmp_path)

    result = load_result(tmp_path, DRAFTER, CHECKER, SETS, reader_set=sentences)
    text = render(result, None)

    assert result.reader is not None and result.reader.stale_passed == ["h-1"]
    assert "## The reader of wordings" in text
    assert "| Held out | 1 of 2 | 0 of 2 |" in text
    assert '- `h-1` (held_out): "W1."' in text
    # Made with no fingerprint, so every part of each is reported as changed.
    assert any("reader.prompt" in line for line in result.stale)
    write(read(sentences.sentences[0], CUT, 3), tmp_path)
    cut = load_result(tmp_path, DRAFTER, CHECKER, SETS, reader_set=sentences)
    assert "1 readings by the reader did not finish" in not_whole(cut)


def test_a_reading_of_a_sentence_that_is_not_in_the_set_fails_score(tmp_path: Path) -> None:
    from evals.cases import SETS
    from evals.result import load_result

    write(read(sentence(name="gone"), NOW_OFF), tmp_path)

    result = load_result(tmp_path, DRAFTER, CHECKER, SETS, reader_set=_sentences())

    assert any("gone" in line for line in result.stale)
