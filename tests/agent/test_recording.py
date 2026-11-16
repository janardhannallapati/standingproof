from pathlib import Path

from standingproof.agent.recording import (
    RecordingChecker,
    RecordingModel,
    ReplayChecker,
    ReplayModel,
)
from standingproof.agent.types import AnswerDraft
from tests.agent.fakes import (
    ScriptedChecker,
    ScriptedModel,
    checker_says,
    disputes,
    finishes,
    search,
    wants_tools,
)


async def test_recorded_turns_replay_identically(tmp_path: Path) -> None:
    draft = AnswerDraft(status="answered", answer="Yes.", citations=["LIB-MFA-01"], note="")
    cassette = tmp_path / "run.json"
    recorder = RecordingModel(
        ScriptedModel([wants_tools(search("c1", "MFA")), finishes(draft)]), cassette
    )
    first = await recorder.complete(system="s", messages=[], tools=[])
    second = await recorder.complete(system="s", messages=[], tools=[])

    replay = ReplayModel(cassette)
    replayed_first = await replay.complete(system="s", messages=[], tools=[])
    replayed_second = await replay.complete(system="s", messages=[], tools=[])

    assert replayed_first.tool_calls == first.tool_calls
    assert replayed_first.usage == first.usage
    assert replayed_second.draft == second.draft
    assert replay.calls == 2


async def test_recorded_checker_replies_replay_identically(tmp_path: Path) -> None:
    cassette = tmp_path / "check.json"
    recorder = RecordingChecker(
        ScriptedChecker([checker_says("not_supported", disputes("and contractors"))]), cassette
    )
    recorded = await recorder.check(system="s", messages=[])

    replay = ReplayChecker(cassette)
    replayed = await replay.check(system="s", messages=[])

    assert replayed == recorded
    assert replay.calls == 1
