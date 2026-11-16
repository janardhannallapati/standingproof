"""The watcher's pass: deliveries, settings that are due, and what the looks found."""

import asyncio
import datetime as dt
import uuid
from typing import Any

from standingproof.checks.service import run_check, utc_now
from standingproof.config import Settings
from standingproof.drift import deliveries
from standingproof.drift.github_events import read
from standingproof.drift.watcher import Watcher, WatcherDeps
from standingproof.models import Connection, Evidence, WebhookDelivery
from tests.conftest import Db
from tests.drift.github_fake import REPO, FakeGitHub
from tests.drift.rig import all_rows, answers, item_row
from tests.workflow.seed import tenant, wf
from tests.workflow.test_review import decide

__all__ = ["wf"]

CHECK = "github.branch_protection"
PAYLOAD = {"action": "edited", "repository": {"full_name": REPO}}


class Scene:
    """One tenant with an approved answer that rests on a look at the fake GitHub."""

    def __init__(self, db: Db, **settings: Any) -> None:
        self.db = db
        self.github = FakeGitHub()
        self.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            github_orgs=settings.pop("github_orgs", "acme"),
            webhook_settle_seconds=settings.pop("webhook_settle_seconds", 0),
            # Off unless a test is about it: a look was made a moment ago, to draft from.
            webhook_min_gap_seconds=settings.pop("webhook_min_gap_seconds", 0),
            **settings,
        )
        self.ahead = dt.timedelta(0)
        self.announced: list[uuid.UUID] = []
        self.owner = tenant(db)
        db.run(self._connect())
        first = db.run(self._look())
        self.run_id, [self.item] = answers(db, self.owner, first.id)
        decide(db, self.owner, self.item, "approve")

    async def _connect(self) -> None:
        async with self.db.sessions() as session:
            session.add(Connection(tenant_id=self.owner, system="github", scope="acme"))
            await session.commit()

    async def _look(self) -> Evidence:
        async with self.db.sessions() as session:
            return await run_check(
                session,
                tenant_id=self.owner,
                name=CHECK,
                scope=REPO,
                backends=self.github.backends(),
                settings=self.settings,
                max_age=dt.timedelta(0),
            )

    def watcher(self) -> Watcher:
        async def announce(run_id: uuid.UUID) -> None:
            self.announced.append(run_id)

        return Watcher(
            WatcherDeps(
                settings=self.settings,
                sessions=self.db.sessions,
                backends=self.github.backends(),
                announce=announce,
                clock=lambda: utc_now() + self.ahead,
            )
        )

    def deliver(self, count: int = 1) -> None:
        async def go() -> None:
            for _ in range(count):
                async with self.db.sessions() as session:
                    await deliveries.store(
                        session,
                        delivery_id=str(uuid.uuid4()),
                        event="branch_protection_rule",
                        reading=read("branch_protection_rule", PAYLOAD),
                        now=utc_now(),
                    )

        self.db.run(go())

    def tick(self) -> None:
        self.db.run(self.watcher().tick())

    def looks(self) -> list[Evidence]:
        return sorted(all_rows(self.db, Evidence), key=lambda row: row.observed_at)

    def outcomes(self) -> list[str | None]:
        return [row.outcome for row in all_rows(self.db, WebhookDelivery)]


def test_a_delivery_makes_the_watcher_look_and_the_answer_is_reopened(wf: Db) -> None:
    scene = Scene(wf)
    scene.github.reviews = None
    scene.deliver()

    scene.tick()

    assert scene.outcomes() == ["looked"]
    assert len(scene.looks()) == 2
    row = item_row(wf, scene.item)
    assert (row.review, row.reopened_at is not None) == ("pending", True)
    assert scene.announced == [scene.run_id]


def test_several_deliveries_for_one_setting_are_answered_by_one_look(wf: Db) -> None:
    scene = Scene(wf)
    scene.deliver(count=5)

    scene.tick()
    scene.tick()

    assert scene.outcomes() == ["looked"] * 5
    assert len(scene.looks()) == 2


def test_a_setting_is_not_looked_at_again_inside_the_gap_and_the_last_delivery_gets_its_look(
    wf: Db,
) -> None:
    scene = Scene(wf, webhook_min_gap_seconds=30)
    scene.ahead = dt.timedelta(seconds=60)
    scene.deliver()
    scene.tick()
    assert len(scene.looks()) == 2

    scene.ahead = dt.timedelta(0)
    scene.deliver()
    scene.tick()
    assert (len(scene.looks()), sorted(map(str, scene.outcomes()))) == (2, ["None", "looked"])

    scene.ahead = dt.timedelta(seconds=60)
    scene.tick()
    assert (len(scene.looks()), scene.outcomes()) == (3, ["looked", "looked"])


def test_a_delivery_is_left_until_the_change_has_had_time_to_show(wf: Db) -> None:
    scene = Scene(wf, webhook_settle_seconds=5)
    scene.deliver()

    scene.tick()
    assert (scene.outcomes(), len(scene.looks())) == ([None], 1)

    scene.ahead = dt.timedelta(seconds=40)
    scene.tick()
    assert scene.outcomes() == ["looked"]


def test_a_look_made_since_the_delivery_arrived_answers_it(wf: Db) -> None:
    scene = Scene(wf, recheck_seconds=10**9)
    scene.deliver()
    wf.run(scene._look())

    scene.tick()

    assert (scene.outcomes(), len(scene.looks())) == (["looked"], 2)


def test_a_setting_is_looked_at_when_it_is_due_and_not_before(wf: Db) -> None:
    scene = Scene(wf)
    scene.github.reviews = 1
    scene.tick()
    assert (len(scene.looks()), item_row(wf, scene.item).review) == (1, "approved")

    scene.ahead = dt.timedelta(days=1, minutes=1)
    scene.tick()

    assert len(scene.looks()) == 2
    assert item_row(wf, scene.item).review == "pending"
    assert scene.announced == [scene.run_id]


def test_a_look_that_cannot_reach_github_reopens_nothing_and_is_tried_again(wf: Db) -> None:
    scene = Scene(wf)
    scene.github.down = True
    scene.ahead = dt.timedelta(days=1, minutes=1)

    scene.tick()
    # Straight afterwards it is not due again: a failed look is retried an hour later.
    scene.ahead = dt.timedelta(0)
    scene.tick()

    assert [row.status for row in scene.looks()] == ["observed", "unavailable"]
    assert item_row(wf, scene.item).review == "approved"


def test_a_scope_the_operator_no_longer_declares_is_not_looked_at(wf: Db) -> None:
    scene = Scene(wf)
    scene.settings = scene.settings.model_copy(update={"github_orgs": ""})
    before = scene.github.requests
    scene.deliver()

    scene.tick()

    assert scene.github.requests == before
    assert scene.looks()[-1].status == "denied"
    assert (scene.outcomes(), item_row(wf, scene.item).review) == (["looked"], "approved")


def test_two_watchers_at_once_make_one_look_for_one_delivery(wf: Db) -> None:
    scene = Scene(wf)
    scene.github.reviews = None
    scene.deliver()

    async def both() -> None:
        await asyncio.gather(*(scene.watcher().tick() for _ in range(3)))

    wf.run(both())

    assert (len(scene.looks()), scene.outcomes()) == (2, ["looked"])
    assert item_row(wf, scene.item).review == "pending"


def test_the_loop_survives_a_pass_that_fails_and_stops_when_told(wf: Db) -> None:
    scene = Scene(wf, drift_poll_seconds=0.01)
    scene.github.reviews = None
    scene.deliver()
    watcher = scene.watcher()
    tick = watcher.tick
    passes = 0

    async def fails_once() -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("the database went away")
        await tick()

    watcher.tick = fails_once  # type: ignore[method-assign]

    async def run_briefly() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(watcher.run(stop))
        for _ in range(200):
            if scene.announced:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    wf.run(run_briefly())

    assert passes >= 2
    assert item_row(wf, scene.item).review == "pending"
