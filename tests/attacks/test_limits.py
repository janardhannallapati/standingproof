"""The day's spend ceiling and the stop switch: what gets through, and what does not."""

import argparse
import asyncio
import datetime as dt
import random
import uuid
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import HandoffReason
from standingproof.checks.service import run_check
from standingproof.limits import switch
from standingproof.limits.__main__ import parser
from standingproof.limits.__main__ import run as limits_command
from standingproof.limits.guard import CallRefusedError, Guard, GuardedModel
from tests.attacks.test_audit_chain import checked, entries
from tests.conftest import Db, no_backends, settings_for
from tests.workflow.rig import CountingModel, Rig
from tests.workflow.seed import items, questionnaire, start, tenant, wf

__all__ = ["wf"]


def a_day() -> dt.date:
    """A day of its own for each test, so no test counts another's spending."""
    return dt.date(2100, 1, 1) + dt.timedelta(days=random.randrange(10**6))  # noqa: S311


def guard(db: Db, redis_url: str, **settings: Any) -> Guard:
    redis = Redis.from_url(redis_url)
    db.closing(redis.aclose)
    day = a_day()
    return Guard(redis, db.database, settings_for(db.url, **settings), today=lambda: day)


def operator(
    db: Db, tenant_id: uuid.UUID | None, *, stopped: bool | None = None, **more: Any
) -> None:
    async def go() -> None:
        async with db.database.across_tenants() as session:
            if stopped is not None:
                await switch.set_stopped(session, tenant_id, stopped, by="priya")
            if "usd" in more:
                await switch.set_daily_limit(session, tenant_id, more["usd"], by="priya")

    db.run(go())


def refusal(db: Db, guarding: Guard, tenant_id: uuid.UUID) -> HandoffReason | None:
    async def go() -> HandoffReason | None:
        try:
            await guarding.reserve(tenant_id)
        except CallRefusedError as refused:
            return refused.reason
        return None

    return db.run(go())


def test_a_call_that_would_pass_the_tenants_ceiling_is_not_made(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    guarding = guard(wf, redis_url, tenant_daily_limit_usd=0.30, call_reserve_usd=0.10)
    model = CountingModel()
    guarded = GuardedModel(model, guarding, owner)

    async def four_calls() -> list[str]:
        ends = []
        for _ in range(4):
            try:
                await guarded.complete(system="", messages=[], tools=[])
                ends.append("called")
            except CallRefusedError as refused:
                ends.append(refused.reason.value)
        return ends

    # Each call is counted as 10 cents until it returns and its real cost (a fraction of a
    # cent here) replaces that. So all four fit under 30 cents, one after another...
    assert wf.run(four_calls()) == ["called"] * 4
    spent, _ = wf.run(guarding.spent_today(owner))
    assert Decimal(0) < spent < Decimal("0.05")
    # ...and with the ceiling at what has been spent, the next one is refused unmade.
    operator(wf, owner, usd=spent)
    assert wf.run(four_calls()) == ["daily_limit"] * 4
    assert model.calls == 4


def test_calls_starting_together_cannot_all_see_the_same_room(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    guarding = guard(wf, redis_url, tenant_daily_limit_usd=0.50, call_reserve_usd=0.10)

    async def twenty() -> list[bool]:
        async def one() -> bool:
            try:
                await guarding.reserve(owner)
            except CallRefusedError:
                return False
            return True

        return list(await asyncio.gather(*(one() for _ in range(20))))

    assert sum(wf.run(twenty())) == 5
    assert wf.run(guarding.spent_today(owner))[0] == Decimal("0.50")


def test_the_deployments_ceiling_counts_every_tenant(wf: Db, redis_url: str) -> None:
    ours, theirs = tenant(wf, "ours"), tenant(wf, "theirs")
    guarding = guard(wf, redis_url, daily_limit_usd=0.20, call_reserve_usd=0.10)

    assert refusal(wf, guarding, ours) is None
    assert refusal(wf, guarding, theirs) is None
    assert refusal(wf, guarding, ours) is HandoffReason.DAILY_LIMIT
    # An operator's ceiling replaces the setting's.
    operator(wf, None, usd=Decimal("0.30"))
    assert refusal(wf, guarding, theirs) is None


def test_a_failed_call_stays_counted_in_full_and_a_new_day_starts_at_zero(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    day = [a_day()]
    redis = Redis.from_url(redis_url)
    wf.closing(redis.aclose)
    settings = settings_for(wf.url, call_reserve_usd=0.10)
    guarding = Guard(redis, wf.database, settings, today=lambda: day[0])

    async def fails() -> Any:
        raise ModelCallError("rate limited", retryable=True)

    async def call() -> None:
        with pytest.raises(ModelCallError):
            await GuardedModel(CountingModel(fails), guarding, owner).complete(
                system="", messages=[], tools=[]
            )
        await guarding.reserve(owner)

    wf.run(call())
    # What a failed call cost is not known, so its whole reservation stays counted, beside
    # the second one's.
    assert wf.run(guarding.spent_today(owner))[0] == Decimal("0.20")
    day[0] += dt.timedelta(days=1)
    assert wf.run(guarding.spent_today(owner)) == (Decimal(0), Decimal(0))


def test_with_the_counter_unreachable_no_call_is_made(wf: Db) -> None:
    owner = tenant(wf)
    nowhere = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    wf.closing(nowhere.aclose)
    model = CountingModel()
    guarded = GuardedModel(model, Guard(nowhere, wf.database, settings_for(wf.url)), owner)

    async def call() -> bool:
        with pytest.raises(ModelCallError) as failed:
            await guarded.complete(system="", messages=[], tools=[])
        return failed.value.retryable

    # Retryable: our own counter being away passes, and a person could not fix it.
    assert wf.run(call()) is True
    assert model.calls == 0


@pytest.mark.parametrize("scope", ["tenant", "deployment"])
def test_the_switch_stops_model_calls_and_looks_at_once_and_resume_restores_them(
    wf: Db, redis_url: str, scope: str
) -> None:
    owner, other = tenant(wf, "ours"), tenant(wf, "theirs")
    guarding = guard(wf, redis_url)
    target = owner if scope == "tenant" else None

    async def look(tenant_id: uuid.UUID) -> tuple[str, str | None]:
        async with wf.database.tenant(tenant_id)() as session:
            seen = await run_check(
                session,
                tenant_id=tenant_id,
                name="github.org_two_factor",
                scope="acme",
                backends=no_backends(),
                settings=settings_for(wf.url, github_orgs="acme"),
            )
            return seen.status, seen.reason

    operator(wf, target, stopped=True)

    assert refusal(wf, guarding, owner) is HandoffReason.STOPPED
    assert wf.run(look(owner)) == ("denied", "stopped")
    # Stopping one tenant leaves the others working; stopping the deployment stops all.
    expected = None if scope == "tenant" else HandoffReason.STOPPED
    assert refusal(wf, guarding, other) is expected

    operator(wf, target, stopped=False)

    assert refusal(wf, guarding, owner) is None
    assert wf.run(look(owner))[1] != "stopped"


def test_a_stopped_question_waits_and_goes_on_after_resume(wf: Db, redis_url: str) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())
    operator(wf, owner, stopped=True)
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.attempts, rig.model.calls) == ("queued", 0, 0)

    operator(wf, owner, stopped=False)
    wf.run(rig.worker.sweep())
    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason, rig.model.calls) == ("needs_review", "no_approved_source", 1)


def test_a_question_at_the_days_ceiling_goes_to_a_person_with_the_reason(
    wf: Db, redis_url: str
) -> None:
    owner = tenant(wf)
    started = start(wf, owner, questionnaire(wf, owner, ["Do you test backups?"]))
    rig = Rig(wf, redis_url, CountingModel())
    operator(wf, owner, usd=Decimal(0))
    rig.add(started.item_ids)

    rig.work_through_the_queue()

    [row] = items(wf, started.run.id)
    assert (row.status, row.reason, rig.model.calls) == ("needs_review", "daily_limit", 0)


def test_every_change_by_an_operator_is_in_the_deployments_audit_chain(
    wf: Db, redis_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = tenant(wf, "ours")
    redis = Redis.from_url(redis_url)
    wf.closing(redis.aclose)
    settings = settings_for(wf.url)

    def command(*words: str) -> argparse.Namespace:
        return parser().parse_args(list(words))

    for words in (
        ("stop", "--by", "priya", "--tenant", "ours"),
        ("resume", "--by", "priya", "--tenant", "ours"),
        ("daily", "--by", "priya", "--usd", "2.50"),
        ("stop", "--by", "omar"),
    ):
        assert wf.run(limits_command(wf.database, redis, settings, command(*words))) == 0
    assert wf.run(limits_command(wf.database, redis, settings, command("show"))) == 0

    found = [(row.actor, row.action, row.subject) for row in entries(wf, "WHERE tenant_id IS NULL")]
    assert found == [
        ("priya", "limits.stop", f"tenant:{owner}"),
        ("priya", "limits.resume", f"tenant:{owner}"),
        ("priya", "limits.daily", "deployment"),
        ("omar", "limits.stop", "deployment"),
    ]
    assert checked(wf, None) == (4, None)
    shown = capsys.readouterr().out
    assert "tenant ours: STOPPED, $0.00 of $10.00 today" in shown
    assert "of $2.50 today (UTC)" in shown


def test_a_tenant_cannot_change_its_own_switch_or_ceiling_or_see_anothers(wf: Db) -> None:
    ours, theirs = tenant(wf, "ours"), tenant(wf, "theirs")
    operator(wf, ours, stopped=True)
    operator(wf, theirs, usd=Decimal("1.00"))

    async def go() -> tuple[list[Any], list[str]]:
        refused = []
        async with wf.database.tenant(ours)() as session:
            seen = list((await session.execute(text("SELECT tenant_id FROM limits"))).all())
        for sql in (
            "UPDATE limits SET stopped = false",
            "UPDATE limits SET daily_limit_usd = 1000",
            "DELETE FROM limits",
            "INSERT INTO limits (tenant_id, changed_by) VALUES (NULL, 'us')",
        ):
            async with wf.database.tenant(ours)() as session:
                with pytest.raises(DBAPIError) as error:
                    await session.execute(text(sql))
                refused.append(str(error.value.orig))
        return seen, refused

    seen, refused = wf.run(go())

    assert seen == [(ours,)]
    assert all("permission denied" in reason for reason in refused)
