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
        # How far the watcher's clock runs ahead of the clock that stamps deliveries. This
        # machine's clock is stepped back by a second or two now and then. A delivery then
        # looks as if it arrived in the future and, correctly, waits for the next pass. A
        # test that makes one pass is not about that, so its watcher runs a little ahead.
        # A test about settling sets its own.
        settled_at_once = self.settings.webhook_settle_seconds == 0
        self.ahead = dt.timedelta(seconds=5 if settled_at_once else 0)
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
                database=self.db.database,
                backends=self.github.backends(),
                announce=announce,
                clock=lambda: utc_now() + self.ahead,
            )
        )

    def deliver(self, count: int = 1, *, at: dt.datetime | None = None) -> None:
        async def go() -> None:
            for _ in range(count):
                async with self.db.sessions() as session:
                    await deliveries.store(
                        session,
                        delivery_id=str(uuid.uuid4()),
                        event="branch_protection_rule",
                        reading=read("branch_protection_rule", PAYLOAD),
                        now=at or utc_now(),
                    )

        self.db.run(go())

    def tick(self) -> None:
        self.db.run(self.watcher().tick())

    def looks(self) -> list[Evidence]:
        return sorted(all_rows(self.db, Evidence), key=lambda row: row.seq)

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
    # It arrived a minute before the look: longer than a look can take, so the look began
    # after it and saw whatever it announced.
    scene.deliver(at=utc_now() - dt.timedelta(minutes=1))
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


def test_the_process_announces_a_reopening_on_the_runs_channel_and_stops_on_a_signal(
    wf: Db, migrated_postgres_url: str, redis_url: str, monkeypatch: Any
) -> None:
    import os
    import signal

    from pydantic import SecretStr
    from redis.asyncio import Redis

    from standingproof.drift import __main__ as process
    from standingproof.workflow.events import CHANGED, channel

    scene = Scene(wf, drift_poll_seconds=0.01)
    scene.github.reviews = None
    scene.deliver()
    settings = scene.settings.model_copy(
        update={
            "database_url": SecretStr(migrated_postgres_url),
            "redis_url": SecretStr(redis_url),
        }
    )
    monkeypatch.setattr(process, "build_backends", lambda _: scene.github.backends())

    async def run_until_announced() -> list[bytes]:
        listener = Redis.from_url(redis_url)
        heard: list[bytes] = []
        try:
            async with listener.pubsub() as pubsub:
                await pubsub.subscribe(channel(scene.run_id))
                task = asyncio.create_task(process.serve(settings))
                for _ in range(500):
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.02)
                    if message:
                        heard.append(message["data"])
                        break
                # The way a container is stopped.
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.wait_for(task, timeout=5)
        finally:
            await listener.aclose()
        return heard

    assert wf.run(run_until_announced()) == [CHANGED.encode()]
    assert item_row(wf, scene.item).review == "pending"


def test_a_delivery_about_a_setting_no_answer_rests_on_is_not_looked_at(wf: Db) -> None:
    # Anyone who holds the secret can name any repository of a connected organisation.
    # Each would be a look on the deployment's token and a row of evidence for ever.
    scene = Scene(wf)
    before = scene.github.requests

    async def go() -> None:
        for number in range(5):
            payload = {"repository": {"full_name": f"acme/unwatched-{number}"}}
            async with wf.sessions() as session:
                await deliveries.store(
                    session,
                    delivery_id=str(uuid.uuid4()),
                    event="branch_protection_rule",
                    reading=read("branch_protection_rule", payload),
                    now=utc_now(),
                )

    wf.run(go())
    scene.tick()

    assert scene.github.requests == before
    assert len(scene.looks()) == 1
    assert scene.outcomes() == ["ignored:not_watched"] * 5


def test_waiting_deliveries_are_taken_in_the_order_they_arrived_whatever_their_times(
    wf: Db,
) -> None:
    from tests.workflow.seed import sql

    scene = Scene(wf)
    scene.deliver(2)
    first, second = sorted(all_rows(wf, WebhookDelivery), key=lambda row: row.seq)
    # The first to arrive carries the later time.
    sql(
        wf,
        "UPDATE webhook_deliveries SET received_at = now() + interval '1 minute' WHERE id = :id",
        id=first.id,
    )

    async def next_one() -> list[uuid.UUID]:
        async with wf.sessions() as session:
            found = await deliveries.waiting(
                session, before=utc_now() + dt.timedelta(hours=1), limit=1
            )
            return [row.id for row in found]

    assert wf.run(next_one()) == [first.id]
    assert second.id != first.id


def test_a_delivery_stamped_earlier_than_the_last_look_still_gets_its_look(wf: Db) -> None:
    scene = Scene(wf)
    scene.github.reviews = None
    # The delivery arrives after the look the answer was drafted from, but carries an
    # earlier time: the clock was stepped back, or the API's host runs behind the watcher's.
    scene.deliver(at=utc_now() - dt.timedelta(seconds=30))

    scene.tick()

    # By the clock the old look "already answered" it. It did not: the look came first.
    assert scene.outcomes() == ["looked"]
    assert len(scene.looks()) == 2
    assert item_row(wf, scene.item).review == "pending"


def test_a_look_that_may_have_begun_before_a_delivery_does_not_answer_it(wf: Db) -> None:
    from tests.workflow.seed import sql

    # A look can take up to 15 seconds. Its row is stored when it ends, so it is numbered
    # after a delivery that arrived while it was reading, though it read before the change.
    scene = Scene(wf, check_timeout_seconds=15)
    scene.github.reviews = None
    scene.deliver()
    # Put the delivery before the look in the stored order, as that interleaving would.
    sql(wf, "UPDATE webhook_deliveries SET seq = (SELECT min(seq) FROM evidence) - 1")

    scene.tick()

    # Stored before the look, but only just: the look cannot be trusted to have seen it.
    assert scene.outcomes() == ["looked"]
    assert len(scene.looks()) == 2


def test_a_look_made_well_after_a_delivery_answers_it_with_no_second_look(wf: Db) -> None:
    from tests.workflow.seed import sql

    scene = Scene(wf, check_timeout_seconds=15)
    scene.deliver()
    sql(
        wf,
        "UPDATE webhook_deliveries SET seq = (SELECT min(seq) FROM evidence) - 1, "
        "received_at = now() - interval '1 minute'",
    )

    scene.tick()

    # It arrived a minute before the look ended: longer than any look takes.
    assert scene.outcomes() == ["looked"]
    assert len(scene.looks()) == 1


def test_one_look_answers_every_waiting_delivery_for_its_setting_however_many(wf: Db) -> None:
    # More than one pass's batch: a replayed delivery must not hold up other settings.
    scene = Scene(wf, drift_batch=3)
    scene.deliver(count=10)

    scene.tick()

    assert scene.outcomes() == ["looked"] * 10
    assert len(scene.looks()) == 2


def test_handled_deliveries_are_removed_after_the_keep_time(wf: Db) -> None:
    from tests.workflow.seed import sql

    scene = Scene(wf, webhook_keep_days=30)
    scene.deliver(count=2)
    scene.tick()
    sql(
        wf,
        "UPDATE webhook_deliveries SET handled_at = now() - interval '31 days' "
        "WHERE id = (SELECT id FROM webhook_deliveries LIMIT 1)",
    )
    scene.deliver()

    scene.tick()

    # The old handled one is gone; the recent one and the one just answered remain.
    assert scene.outcomes() == ["looked", "looked"]


def test_a_delivery_that_arrived_after_the_last_look_is_never_marked_without_one(wf: Db) -> None:
    # One look answers the deliveries that came before it, and only those.
    scene = Scene(wf, drift_batch=1, recheck_seconds=10**9)
    scene.deliver(at=utc_now() - dt.timedelta(minutes=1))
    wf.run(scene._look())
    scene.github.reviews = None
    scene.deliver()

    scene.tick()
    assert sorted(map(str, scene.outcomes())) == ["None", "looked"]
    assert len(scene.looks()) == 2

    scene.tick()
    assert scene.outcomes() == ["looked", "looked"]
    assert len(scene.looks()) == 3
    assert item_row(wf, scene.item).review == "pending"


def test_a_delivery_still_settling_is_not_marked_by_a_look_for_an_older_one(wf: Db) -> None:
    scene = Scene(wf, webhook_settle_seconds=5, recheck_seconds=10**9)
    scene.deliver()
    scene.ahead = dt.timedelta(seconds=6)
    scene.tick()
    assert scene.outcomes() == ["looked"]

    # A second one arrives; a pass that starts 6 s after the FIRST is too early for it.
    scene.ahead = dt.timedelta(0)
    scene.deliver()
    scene.deliver()
    wf.run(_age_one_delivery(wf, seconds=10))
    scene.tick()

    assert sorted(map(str, scene.outcomes())) == ["None", "looked", "looked"]


async def _age_one_delivery(db: Db, *, seconds: int) -> None:
    from sqlalchemy import text

    async with db.sessions() as session:
        await session.execute(
            text(
                "UPDATE webhook_deliveries "
                "SET received_at = received_at - make_interval(secs => :s) "
                "WHERE id = (SELECT id FROM webhook_deliveries WHERE handled_at IS NULL LIMIT 1)"
            ),
            {"s": seconds},
        )
        await session.commit()
