"""A check is kept: one row per wording checked. Real Postgres and Redis, scripted turns."""

import uuid
from typing import Any

import anthropic
import pytest
from sqlalchemy import select

from standingproof.agent.pricing import UnknownModelError
from standingproof.agent.types import AnswerDraft
from standingproof.config import Settings
from standingproof.models import DraftCheck
from standingproof.workflow import results, review
from standingproof.workflow.__main__ import build_checker
from standingproof.workflow.state import Outcome
from tests.agent.fakes import (
    ScriptedChecker,
    ScriptedModel,
    checker_says,
    disputes,
    finishes,
    search,
    wants_tools,
)
from tests.conftest import Db
from tests.workflow.seed import approved_answer, items, questionnaire, start, tenant, wf
from tests.workflow.test_worker import Rig

__all__ = ["wf"]

QUESTION = "Is MFA enforced for employees and contractors?"
WORDING = "Yes. MFA is enforced for all staff and contractors."


def checks(db: Db, item_id: uuid.UUID) -> list[DraftCheck]:
    async def load() -> list[DraftCheck]:
        async with db.sessions() as session:
            rows = await session.execute(
                select(DraftCheck)
                .where(DraftCheck.run_question_id == item_id)
                .order_by(DraftCheck.checked_at)
            )
            return list(rows.scalars())

    return db.run(load())


class Setup:
    """One question with one approved answer that is close to it but not the same."""

    def __init__(self, db: Db) -> None:
        self.owner = tenant(db)
        answer_id = approved_answer(db, self.owner, "Is MFA enforced?", "Yes, for all staff.")
        self.source = f"answer:{answer_id}"
        self.started = start(db, self.owner, questionnaire(db, self.owner, [QUESTION]))

    def drafter(self, *wordings: str) -> ScriptedModel:
        turns: list[Any] = [wants_tools(search("c1", "MFA enforced"))]
        turns += [finishes(self.draft(wording)) for wording in wordings]
        return ScriptedModel(turns)

    def draft(self, wording: str) -> AnswerDraft:
        return AnswerDraft(status="answered", answer=wording, citations=[self.source], note="")


def test_a_disputed_draft_is_saved_for_a_person_with_one_check_row(wf: Db, redis_url: str) -> None:
    setup = Setup(wf)
    checker = ScriptedChecker(
        [checker_says("not_supported", disputes("and contractors", setup.source))]
    )
    rig = Rig(wf, redis_url, setup.drafter(WORDING), checker=checker)
    rig.add(setup.started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, setup.started.run.id)
    assert (row.status, row.reason, row.path) == ("needs_review", "checker_disagrees", "drafted")
    assert (row.answer, row.review) == (WORDING, "pending")
    assert row.steps == 2
    [check] = checks(wf, row.id)
    assert (check.outcome, check.answer_checked) == ("disagrees", WORDING)
    assert check.tenant_id == setup.owner
    assert check.model == "claude-sonnet-5-5"
    assert check.cost_usd > 0
    assert check.findings == [
        {
            "kind": "unsupported",
            "quote": "and contractors",
            "located": True,
            "source_id": setup.source,
            "explanation": "Not in the source.",
        }
    ]
    # The check was paid for by this question: its cost is in the question's total.
    assert row.cost_usd > check.cost_usd
    rig.close()


def test_a_draft_the_checker_agrees_with_is_ready_and_the_check_is_kept(
    wf: Db, redis_url: str
) -> None:
    setup = Setup(wf)
    rig = Rig(
        wf,
        redis_url,
        setup.drafter(WORDING),
        checker=ScriptedChecker([checker_says("supported")]),
    )
    rig.add(setup.started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, setup.started.run.id)
    assert (row.status, row.reason) == ("ready", None)
    [check] = checks(wf, row.id)
    assert (check.outcome, check.findings) == ("agrees", [])
    rig.close()


def test_a_reused_answer_has_no_check_row(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    approved_answer(wf, owner, "Is MFA enforced?", "Yes, for all staff.")
    started = start(wf, owner, questionnaire(wf, owner, ["is mfa enforced?"]))
    checker = ScriptedChecker([])
    rig = Rig(wf, redis_url, ScriptedModel([]), checker=checker)
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.path) == ("ready", "reused")
    assert checks(wf, row.id) == []
    rig.close()


def test_a_redraft_adds_a_second_row_and_leaves_the_first_alone(wf: Db, redis_url: str) -> None:
    setup = Setup(wf)
    second = "Yes. MFA is enforced for all staff."
    checker = ScriptedChecker(
        [
            checker_says("not_supported", disputes("and contractors", setup.source)),
            checker_says("supported"),
        ]
    )
    rig = Rig(wf, redis_url, setup.drafter(WORDING, second), checker=checker)
    rig.add(setup.started.item_ids)
    rig.work_through_the_queue()
    [row] = items(wf, setup.started.run.id)

    async def send_back() -> None:
        async with wf.sessions() as session:
            await review.decide(
                session,
                tenant_id=setup.owner,
                item_id=row.id,
                decision="redraft",
                version=row.version,
                reviewer="asha",
                comment="Leave contractors out.",
                max_redrafts=2,
                max_chars=4000,
            )

    wf.run(send_back())
    rig.add([row.id])
    rig.work_through_the_queue()

    [after] = items(wf, setup.started.run.id)
    assert (after.status, after.answer) == ("ready", second)
    first_check, second_check = checks(wf, row.id)
    assert (first_check.outcome, first_check.answer_checked) == ("disagrees", WORDING)
    assert (second_check.outcome, second_check.answer_checked) == ("agrees", second)
    rig.close()


def test_a_worker_that_lost_its_lease_saves_no_check(wf: Db, redis_url: str) -> None:
    setup = Setup(wf)
    [item_id] = setup.started.item_ids
    from standingproof.agent.steps import not_checked
    from standingproof.agent.types import Usage

    outcome = Outcome(
        status="needs_review",
        path="drafted",
        reason="not_checked",
        draft=setup.draft(WORDING),
        steps=2,
        cost_usd=0.01,
        usage=Usage(),
        check=not_checked(WORDING),
    )

    async def save() -> bool:
        async with wf.sessions() as session:
            return await results.save_result(
                session, item_id=item_id, owner="nobody", outcome=outcome
            )

    assert wf.run(save()) is False
    assert checks(wf, item_id) == []


# --- Which checker a worker gets. ---


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, environment="test", **values)


def client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key="not-a-key")


def test_a_worker_gets_a_checker_by_default() -> None:
    assert build_checker(settings(), client()) is not None


def test_the_checker_can_be_switched_off() -> None:
    assert build_checker(settings(check_enabled=False), client()) is None


def test_a_check_model_with_no_price_is_refused_at_start() -> None:
    with pytest.raises(UnknownModelError):
        build_checker(settings(check_model="some-model-nobody-priced"), client())


def test_the_checker_is_not_the_drafters_model_by_default() -> None:
    assert settings().check_model != settings().draft_model
