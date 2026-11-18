"""The paid command and the reader: what it is sent, what it costs, what it leaves."""

from pathlib import Path

import pytest

from evals.cases import ReaderSet, load_drafter
from evals.fingerprint import Config, of_sentence
from evals.graders import grade_draft
from evals.plan import Plan
from evals.run import READ_AT_MOST
from evals.transcript import DrafterRun, ReaderRun, read_all
from standingproof.agent.loop import ModelCallError
from tests.conftest import Db
from tests.eval.readings import CUT, NOW_OFF, OFF, ReadsPlainly, sentence
from tests.eval.test_run import SENT_BACK, RedraftsFrom, go

DRAFTER = load_drafter()
CONFIG = Config.current()
SENTENCES = ReaderSet(sentences=[sentence(), sentence("does_not", "open", "s-2", "Two reviews.")])
ONLY = Plan(drafter=[], docs_only=[], checker=[], cheaper=[], reader=[("s-1", 1), ("s-2", 1)])


def test_a_sentence_is_read_once_recorded_and_not_paid_for_again(db: Db, tmp_path: Path) -> None:
    manifest, used = go(db, tmp_path, ONLY, sentences=SENTENCES, reader=ReadsPlainly(NOW_OFF))

    runs = read_all(tmp_path)
    assert manifest.complete and manifest.recorded == 2 and used.reader.calls == 2
    assert [(type(run), run.case, run.run) for run in runs] == [
        (ReaderRun, "s-1", 1),
        (ReaderRun, "s-2", 1),
    ]
    first = runs[0]
    assert isinstance(first, ReaderRun) and first.turn == NOW_OFF
    assert first.fingerprint == of_sentence(SENTENCES.sentences[0], DRAFTER.by_name[OFF], CONFIG)
    # 800 tokens in and 100 out on the reader's model.
    assert first.cost_usd == pytest.approx(0.0026)
    assert manifest.spent_usd == pytest.approx(0.0052)

    again, later = go(db, tmp_path, ONLY, sentences=SENTENCES, reader=ReadsPlainly(NOW_OFF))
    assert (later.reader.calls, again.reused, again.spent_usd) == (0, 2, 0)


def test_the_reader_is_sent_the_sentence_and_the_question_and_nothing_of_the_world(
    db: Db, tmp_path: Path
) -> None:
    from evals.reader import READER_PROMPT

    _, used = go(db, tmp_path, ONLY, sentences=SENTENCES, reader=ReadsPlainly(NOW_OFF))

    request = used.reader.requests[0]
    [message] = request["messages"]
    assert request["system"] == READER_PROMPT
    assert "Main is unprotected." in message["content"]
    assert DRAFTER.by_name[OFF].question in message["content"]
    for kept_back in ("states_now", "does_not", "s-1", "held_out", '"reviews": 0', "Shorter"):
        assert kept_back not in message["content"]


def test_a_reading_that_did_not_finish_is_made_again(db: Db, tmp_path: Path) -> None:
    go(db, tmp_path, ONLY, sentences=SENTENCES, reader=ReadsPlainly(CUT))

    _, used = go(db, tmp_path, ONLY, sentences=SENTENCES, reader=ReadsPlainly(NOW_OFF))

    assert used.reader.calls == 2


def test_no_sentence_is_sent_when_the_ceiling_has_no_room_for_a_reading(
    db: Db, tmp_path: Path
) -> None:
    manifest, used = go(
        db, tmp_path, ONLY, ceiling=READ_AT_MOST - 0.01, sentences=SENTENCES, reader=ReadsPlainly()
    )

    assert used.reader.calls == 0
    assert not manifest.complete and "ceiling" in manifest.stopped


def test_a_reader_that_cannot_be_reached_leaves_no_recording(db: Db, tmp_path: Path) -> None:
    down = ReadsPlainly(ModelCallError("rate limited", retryable=True))

    manifest, used = go(db, tmp_path, ONLY, sentences=SENTENCES, reader=down)

    assert not manifest.complete and "could not be reached" in manifest.stopped
    assert used.reader.calls == 2 and read_all(tmp_path) == []
    assert manifest.spent_usd == pytest.approx(2 * READ_AT_MOST)


def test_a_redrafts_wording_is_read_and_the_reading_is_kept_with_the_run(
    db: Db, tmp_path: Path
) -> None:
    reader = ReadsPlainly()
    manifest, _ = go(db, tmp_path, SENT_BACK, model=RedraftsFrom(look_again=True), reader=reader)

    [run] = read_all(tmp_path)
    assert manifest.complete and isinstance(run, DrafterRun)
    assert run.reading is not None and run.reading.reading is not None
    said = {item.fact: item.value for item in run.reading.reading.facts}
    assert said == {"protected": "false", "reviews": "0", "admins": "false"}
    assert grade_draft(DRAFTER.by_name[OFF], run).read is True
    # One reading, of the redraft. The first draft's wording is never sent to the reader.
    [request] = reader.requests
    content = request["messages"][0]["content"]  # type: ignore[index]
    assert "not protected" in content and "2 approving reviews" not in content
    case = DRAFTER.by_name[OFF]
    assert case.redraft is not None and case.redraft.instruction not in content


def test_a_redraft_whose_reading_did_not_finish_is_made_again(db: Db, tmp_path: Path) -> None:
    go(db, tmp_path, SENT_BACK, model=RedraftsFrom(look_again=True), reader=ReadsPlainly(CUT))
    [run] = read_all(tmp_path)
    assert isinstance(run, DrafterRun)
    assert grade_draft(DRAFTER.by_name[OFF], run).read is False

    again, used = go(db, tmp_path, SENT_BACK, model=RedraftsFrom(look_again=True))

    assert again.recorded == 1 and used.reader.calls == 1
