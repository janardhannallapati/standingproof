"""The paid command, with a scripted model: what it asks, what it spends, what it leaves."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals.cases import load_checker, load_drafter
from evals.fingerprint import Config, of_drafter_case
from evals.plan import Plan, approved, estimate, load_manifest, plan_for
from evals.run import CHECK_AT_MOST, DRAFT_CALL_AT_MOST, Clients, run_plan
from evals.transcript import CheckerRun, DrafterRun, read_all
from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import AnswerDraft, CheckTurn, ModelTurn, ToolCall, Usage
from tests.agent.fakes import checker_says, disputes, finishes, wants_tools
from tests.conftest import Db
from tests.eval.readings import ReadsPlainly
from tests.fakes import FakeEmbedder

DRAFTER = load_drafter()
CHECKER = load_checker()
CONFIG = Config.current()
# About $0.012 a turn on the drafter's model: a round number to count with.
TURN = Usage(input_tokens=2000, output_tokens=200)


class LooksThenAnswers:
    """A drafter that looks at branch protection once and repeats what the look said."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def complete(self, *, messages: list[dict[str, Any]], **_: Any) -> ModelTurn:
        self.calls += 1
        if self.fail:
            raise ModelCallError("provider down", retryable=True)
        last = messages[-1]["content"]
        if isinstance(last, str):
            call = ToolCall(
                id="t1",
                name="check_live_setting",
                arguments={"check": "github.branch_protection", "scope": "harborlight/app"},
            )
            return wants_tools(call, tokens=TURN)
        seen = json.loads(last[0]["content"])
        draft = AnswerDraft(
            status="answered", answer=seen["statement"], citations=[seen["id"]], note=""
        )
        return finishes(draft, tokens=TURN)


class Agrees:
    def __init__(self, reply: CheckTurn | Exception | None = None) -> None:
        self.calls = 0
        self.reply = reply or checker_says("supported")

    async def check(self, **_: Any) -> CheckTurn:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


SMALL = Plan(
    drafter=[("b-reviews-two-vs-one", 1), ("a-reviews-two", 1)],
    docs_only=[],
    checker=[("sound-backups", 1), ("wider-contractors", 1)],
    cheaper=["sound-backups"],
)


def clients(
    model: Any = None, checker: Any = None, cheaper: Any = None, reader: Any = None
) -> Clients:
    return Clients(
        model=model or LooksThenAnswers(),
        checker=checker or Agrees(),
        cheaper_checker=cheaper or Agrees(checker_says("not_supported", disputes("daily"))),
        reader=reader or ReadsPlainly(),
    )


def go(db: Db, root: Path, plan: Plan = SMALL, ceiling: float = 4.0, **kinds: Any):  # type: ignore[no-untyped-def]
    sentences = kinds.pop("sentences", None)
    used = clients(**kinds)
    manifest = db.run(
        run_plan(
            plan,
            reader=sentences,
            sessions=db.sessions,
            embedder=FakeEmbedder(),
            clients=used,
            config=CONFIG,
            ceiling=ceiling,
            root=root,
            say=lambda _line: None,
        )
    )
    return manifest, used


def test_the_full_plan_and_its_estimate() -> None:
    plan = plan_for(DRAFTER, CHECKER, smoke=False)

    cost = estimate(plan)

    assert (len(plan.drafter), len(plan.docs_only), len(plan.checker), len(plan.cheaper)) == (
        111,
        18,
        168,
        56,
    )
    # A redraft case is two rounds, so it is counted on its own line at twice the price.
    assert [line.calls for line in cost.lines] == [96, 15, 18, 168, 56, 0]
    assert 2.9 < cost.total < 3.6


def test_one_family_can_be_run_alone_and_fewer_times() -> None:
    plan = plan_for(DRAFTER, CHECKER, smoke=False, family="G", runs=1)

    assert sorted(DRAFTER.by_name[name].family for name, _ in plan.drafter) == list("GGGGG")
    assert {number for _, number in plan.drafter} == {1}
    assert (plan.docs_only, plan.checker, plan.cheaper) == ([], [], [])
    assert [line.calls for line in estimate(plan).lines] == [0, 5, 0, 0, 0, 0]
    assert estimate(plan).total < 0.30


def test_the_smoke_plan_is_one_case_of_each_family_and_six_drafts() -> None:
    plan = plan_for(DRAFTER, CHECKER, smoke=True)

    assert sorted(DRAFTER.by_name[name].family for name, _ in plan.drafter) == list("ABCDEFG")
    assert (len(plan.checker), plan.docs_only, plan.cheaper) == (6, [], [])
    assert estimate(plan).total < 0.30


@pytest.mark.parametrize(
    ("terminal", "typed", "flag", "allowed"),
    [
        (True, "yes", None, True),
        (True, "y", None, False),
        (True, "", None, False),
        (False, "yes", None, False),  # nobody is there to have typed it
        (False, "", 4.0, True),
        (False, "", 3.0, False),  # approved for a different ceiling
        (True, "no", 4.0, True),  # the stated approval is enough; nothing is asked
    ],
)
def test_nothing_runs_without_a_yes(
    terminal: bool, typed: str, flag: float | None, allowed: bool
) -> None:
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return typed

    assert approved(ceiling=4.0, stated=flag, terminal=terminal, ask=ask) is allowed
    assert bool(asked) == (terminal and flag is None)


def test_a_run_leaves_one_recording_per_case_and_a_manifest(db: Db, tmp_path: Path) -> None:
    manifest, used = go(db, tmp_path)

    runs = read_all(tmp_path)
    drafts = [run for run in runs if isinstance(run, DrafterRun)]
    checks = [run for run in runs if isinstance(run, CheckerRun)]
    assert {(d.case, d.variant) for d in drafts} == {
        ("b-reviews-two-vs-one", "full"),
        ("a-reviews-two", "full"),
    }
    assert {(c.case, c.variant) for c in checks} == {
        ("sound-backups", "main"),
        ("wider-contractors", "main"),
        ("sound-backups", "cheaper"),
    }
    first = next(d for d in drafts if d.case == "b-reviews-two-vs-one")
    assert first.final_draft is not None and "1 approving review " in first.final_draft.answer
    assert [use.name for use in first.tool_uses] == ["check_live_setting"]
    assert first.tool_uses[0].source_ids == first.final_draft.citations
    assert list(first.seen_sources) == first.final_draft.citations
    assert first.check_turn is not None and first.steps == 2
    assert first.fingerprint == of_drafter_case(
        DRAFTER, DRAFTER.by_name[first.case], "full", CONFIG
    )
    assert manifest.complete and manifest == load_manifest(tmp_path)
    assert manifest.spent_usd == pytest.approx(sum(run.cost_usd for run in runs))
    assert manifest.spent_usd > 0
    assert (used.model.calls, used.checker.calls, used.cheaper_checker.calls) == (4, 4, 1)


def test_the_cheaper_reading_is_recorded_with_its_own_model(db: Db, tmp_path: Path) -> None:
    go(db, tmp_path)

    [cheaper] = [
        r for r in read_all(tmp_path) if isinstance(r, CheckerRun) and r.variant == "cheaper"
    ]
    assert cheaper.fingerprint["checker.model"] == CONFIG.cheaper_check_model
    assert cheaper.turn is not None and cheaper.turn.verdict is not None


def test_a_run_stops_starting_cases_at_the_ceiling(db: Db, tmp_path: Path) -> None:
    # Room for every call of the first case at its worst, and for none of the second.
    manifest, used = go(db, tmp_path, ceiling=DRAFT_CALL_AT_MOST + 0.02)

    assert not manifest.complete and "ceiling" in manifest.stopped
    assert used.model.calls == 2
    assert manifest.spent_usd <= DRAFT_CALL_AT_MOST + 0.02
    assert len(read_all(tmp_path)) == 1


def test_the_ceiling_is_checked_before_every_call_not_only_every_case(
    db: Db, tmp_path: Path
) -> None:
    class Wordy(LooksThenAnswers):
        async def complete(self, **request: Any) -> ModelTurn:
            turn = await super().complete(**request)
            # 10,000 output tokens: $0.20 on the drafter's model.
            return turn.model_copy(update={"usage": Usage(output_tokens=10_000)})

    ceiling = DRAFT_CALL_AT_MOST + 0.05
    manifest, used = go(db, tmp_path, ceiling=ceiling, model=Wordy())

    # The second turn of the first case could have passed the ceiling, so it was not made.
    assert used.model.calls == 1
    assert manifest.spent_usd == pytest.approx(0.20) and manifest.spent_usd <= ceiling
    assert not manifest.complete and "ceiling" in manifest.stopped
    assert read_all(tmp_path) == []


def test_a_run_that_dies_does_not_leave_the_last_runs_manifest_standing(
    db: Db, tmp_path: Path
) -> None:
    go(db, tmp_path)
    assert load_manifest(tmp_path).complete  # type: ignore[union-attr]

    class Broken(LooksThenAnswers):
        async def complete(self, **request: Any) -> ModelTurn:
            raise RuntimeError("a bug, not a provider failure")

    other = Plan(drafter=[("b-org-2fa-off", 1)], docs_only=[], checker=[], cheaper=[])
    with pytest.raises(RuntimeError):
        go(db, tmp_path, plan=other, model=Broken())

    manifest = load_manifest(tmp_path)
    assert manifest is not None and not manifest.complete and "did not finish" in manifest.stopped


def test_a_case_that_reaches_its_own_limit_before_the_check_is_a_result(
    db: Db, tmp_path: Path
) -> None:
    import dataclasses

    # Two turns cost $0.024, over this limit, so the loop skips the check itself.
    tight = dataclasses.replace(CONFIG, max_cost_usd=0.02)
    plan = Plan(drafter=[("b-reviews-two-vs-one", 1)], docs_only=[], checker=[], cheaper=[])
    used = clients()

    manifest = db.run(
        run_plan(
            plan,
            sessions=db.sessions,
            embedder=FakeEmbedder(),
            clients=used,
            config=tight,
            ceiling=4.0,
            root=tmp_path,
            say=lambda _line: None,
        )
    )

    [run] = read_all(tmp_path)
    assert manifest.complete and (used.model.calls, used.checker.calls) == (2, 0)
    assert isinstance(run, DrafterRun) and run.check_turn is None and run.final_draft is not None


def test_what_is_already_recorded_is_not_paid_for_again(db: Db, tmp_path: Path) -> None:
    go(db, tmp_path)

    manifest, used = go(db, tmp_path)

    assert (used.model.calls, used.checker.calls, used.cheaper_checker.calls) == (0, 0, 0)
    assert manifest.complete and manifest.spent_usd == 0


def test_a_recording_made_from_other_inputs_is_made_again(db: Db, tmp_path: Path) -> None:
    go(db, tmp_path)
    stale = tmp_path / "checker-main" / "sound-backups.1.json"
    data = json.loads(stale.read_text())
    data["fingerprint"]["checker.prompt"] = "0000"
    stale.write_text(json.dumps(data))

    _, used = go(db, tmp_path)

    assert (used.model.calls, used.checker.calls) == (0, 1)


def test_a_recording_whose_check_did_not_finish_is_made_again(db: Db, tmp_path: Path) -> None:
    cut_short = Agrees(checker_says(None, stop_reason="max_tokens"))
    go(db, tmp_path, checker=cut_short)

    _, used = go(db, tmp_path)

    # Both drafter cases and both labelled drafts were read by the cut-short checker.
    assert (used.model.calls, used.checker.calls, used.cheaper_checker.calls) == (4, 4, 0)


def test_any_error_from_a_request_is_counted_as_if_it_was_billed(db: Db, tmp_path: Path) -> None:
    class Garbled(LooksThenAnswers):
        async def complete(self, **request: Any) -> ModelTurn:
            raise ValueError("the reply did not validate")

    with pytest.raises(ValueError):
        go(db, tmp_path, model=Garbled())

    manifest = load_manifest(tmp_path)
    assert manifest is not None and manifest.spent_usd == pytest.approx(DRAFT_CALL_AT_MOST)


def test_a_provider_failure_with_no_room_to_retry_says_so(db: Db, tmp_path: Path) -> None:
    manifest, used = go(
        db, tmp_path, ceiling=DRAFT_CALL_AT_MOST + 0.1, model=LooksThenAnswers(fail=True)
    )

    assert used.model.calls == 1
    assert "provider" in manifest.stopped and not manifest.complete


def test_a_provider_failure_is_not_recorded_as_a_result(db: Db, tmp_path: Path) -> None:
    manifest, used = go(db, tmp_path, model=LooksThenAnswers(fail=True))

    assert not manifest.complete and "provider" in manifest.stopped
    assert used.model.calls == 2  # tried, tried once more, then stopped the whole run
    # A request that failed may still have been billed, so it counts at the most it could cost.
    assert manifest.spent_usd == pytest.approx(2 * DRAFT_CALL_AT_MOST)
    assert [run for run in read_all(tmp_path) if isinstance(run, DrafterRun)] == []


def test_a_check_that_could_not_be_made_is_not_recorded_either(db: Db, tmp_path: Path) -> None:
    down = Agrees(ModelCallError("provider down", retryable=True))

    manifest, _ = go(db, tmp_path, checker=down)

    assert not manifest.complete
    assert read_all(tmp_path) == []


def test_money_spent_on_an_attempt_that_failed_still_counts(db: Db, tmp_path: Path) -> None:
    down = Agrees(ModelCallError("provider down", retryable=True))

    manifest, used = go(db, tmp_path, checker=down)

    # Two attempts, two drafter turns each, were paid for before the check failed.
    assert used.model.calls == 4
    assert manifest.spent_usd == pytest.approx(4 * 0.012 + 2 * CHECK_AT_MOST)


def test_a_recording_that_may_not_be_written_stops_the_run_cleanly(db: Db, tmp_path: Path) -> None:
    class Leaks(LooksThenAnswers):
        async def complete(self, **request: Any) -> ModelTurn:
            turn = await super().complete(**request)
            if turn.draft is not None:
                leaked = turn.draft.model_copy(update={"note": "key AKIAIOSFODNN7EXAMPLE"})
                turn = turn.model_copy(update={"draft": leaked})
            return turn

    manifest, _ = go(db, tmp_path, model=Leaks())

    assert not manifest.complete and "credential" in manifest.stopped
    assert manifest.recorded == 0 and read_all(tmp_path) == []
    assert manifest.spent_usd > 0


class RedraftsFrom:
    """A drafter that looks once and answers; sent back, it either answers again from the
    look it already has (`look_again=False`) or looks at the setting again first."""

    def __init__(self, look_again: bool) -> None:
        self.look_again = look_again
        self.requests: list[dict[str, Any]] = []

    async def complete(self, *, messages: list[dict[str, Any]], **more: Any) -> ModelTurn:
        self.requests.append({"messages": messages, **more})
        looks = [
            json.loads(block["content"])
            for message in messages
            if isinstance(message["content"], list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        sent_back = more.get("system_addendum") is not None
        wanted = 2 if sent_back and self.look_again else 1
        if len(looks) < wanted:
            call = ToolCall(
                id=f"t{len(looks) + 1}",
                name="check_live_setting",
                arguments={"check": "github.branch_protection", "scope": "harborlight/app"},
            )
            return wants_tools(call, tokens=TURN)
        seen = looks[-1]
        draft = AnswerDraft(
            status="answered", answer=seen["statement"], citations=[seen["id"]], note=""
        )
        return finishes(draft, tokens=TURN)


SENT_BACK = Plan(
    drafter=[("g-reviews-switched-off", 1)],
    docs_only=[],
    checker=[],
    cheaper=[],
    redraft=frozenset({"g-reviews-switched-off"}),
)


def sent_back(db: Db, root: Path, look_again: bool) -> tuple[DrafterRun, RedraftsFrom]:
    model = RedraftsFrom(look_again)
    manifest, _ = go(db, root, SENT_BACK, model=model)
    assert manifest.complete
    [run] = [run for run in read_all(root) if isinstance(run, DrafterRun)]
    return run, model


def test_a_redraft_case_is_drafted_then_sent_back_through_the_products_own_graph(
    db: Db, tmp_path: Path
) -> None:
    from standingproof.agent.prompts import REDRAFT_REQUEST

    run, model = sent_back(db, tmp_path, look_again=True)

    case = DRAFTER.by_name["g-reviews-switched-off"]
    assert case.redraft is not None
    # The first draft was written while the branch was protected.
    assert run.first_draft is not None and "2 approving reviews" in run.first_draft.answer
    # The redraft is what the recording describes: a second look, made after the change.
    assert run.final_draft is not None and "not protected" in run.final_draft.answer
    assert [use.name for use in run.tool_uses] == ["check_live_setting"] * 2
    assert run.redraft_from == 1
    assert run.final_draft.citations == run.tool_uses[1].source_ids
    assert run.check_turn is not None and run.handoff is None
    assert run.fingerprint == of_drafter_case(DRAFTER, case, "full", CONFIG)
    # As the product sends a redraft: the reviewer's words in the system message only, and
    # our fixed request as the last message.
    last = model.requests[-1]
    assert case.redraft.instruction in last["system_addendum"]
    assert not any(case.redraft.instruction in str(m["content"]) for m in last["messages"])
    assert {"role": "user", "content": REDRAFT_REQUEST} in last["messages"]
    assert model.requests[0]["system_addendum"] is None


def test_a_redraft_written_from_the_first_look_is_ready_and_wrong(db: Db, tmp_path: Path) -> None:
    from evals.graders import grade_draft

    run, _ = sent_back(db, tmp_path, look_again=False)

    case = DRAFTER.by_name["g-reviews-switched-off"]
    assert run.final_draft is not None and "2 approving reviews" in run.final_draft.answer
    assert len(run.tool_uses) == 1 and run.redraft_from == 1
    graded = grade_draft(case, run)
    # Its own source supports it and the checker agrees, so nothing in the round stops it.
    # The world says otherwise. And it cites a look, but not one the redraft made.
    assert (graded.grade, graded.ready, graded.looked) == ("wrong", True, False)


def test_a_redraft_that_looks_again_is_right(db: Db, tmp_path: Path) -> None:
    from evals.graders import grade_draft

    run, _ = sent_back(db, tmp_path, look_again=True)

    graded = grade_draft(DRAFTER.by_name["g-reviews-switched-off"], run)
    assert (graded.grade, graded.ready, graded.looked) == ("right", True, True)


class GoesAwayWhenSentBack(RedraftsFrom):
    """The provider answers the first draft and is unreachable for the redraft."""

    async def complete(self, *, messages: list[dict[str, Any]], **more: Any) -> ModelTurn:
        if more.get("system_addendum") is not None:
            raise ModelCallError("rate limited", retryable=True)
        return await super().complete(messages=messages, **more)


def test_a_provider_failure_in_a_redraft_stops_the_run_cleanly_and_records_nothing(
    db: Db, tmp_path: Path
) -> None:
    # The graph lets a failure that may pass go up to its caller, where the loop turns
    # every failure into a result. The run must end as it does for any other case.
    manifest, _ = go(db, tmp_path, SENT_BACK, model=GoesAwayWhenSentBack(look_again=True))

    assert not manifest.complete
    assert "could not be reached" in manifest.stopped
    assert [run for run in read_all(tmp_path) if isinstance(run, DrafterRun)] == []
    # Both attempts are counted as if they were billed.
    assert manifest.spent_usd >= 2 * DRAFT_CALL_AT_MOST


def test_smoke_and_one_family_cannot_be_asked_for_together(tmp_path: Path) -> None:
    from evals.__main__ import NOT_APPROVED, main

    said: list[str] = []
    for words in (["estimate", "--smoke", "--family", "G"], ["estimate", "--runs", "1"]):
        assert main(words, root=tmp_path, out=said.append) == NOT_APPROVED
    assert "--smoke" in said[0] and "--family" in said[1]
