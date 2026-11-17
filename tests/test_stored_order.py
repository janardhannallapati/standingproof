"""Order is the order rows were stored in, never what two clocks say.

This machine's clock is stepped back now and then, and a deployment's hosts never agree to
the second. Each test stores two rows, gives the later one the earlier time (or the earlier
one the later time), and expects the answer it had before.
"""

import datetime as dt
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import update

from standingproof.drift import examine
from standingproof.models import Connection, DriftQueue, RunQuestion
from tests.conftest import ClientFactory, Db
from tests.drift.rig import (
    OPEN,
    PROTECTED,
    all_rows,
    answers,
    approved_answer,
    examine_all,
    item_row,
    observe,
)
from tests.drift.test_keep import now_decide, reopened
from tests.drift.test_looks import look
from tests.drift.test_view import record_sent, reopening
from tests.test_checks_api import ORG_2FA, backends, connect, run
from tests.test_files_api import decided_run
from tests.test_review_api import add_check, decide, run_with_drafts
from tests.workflow.seed import sql, tenant, wf
from tests.workflow.test_review import decide as decide_as
from tests.workflow.test_review import drafted
from tests.workflow.test_runs import take

__all__ = ["wf"]


def test_the_model_gets_the_redraft_instruction_stored_last(wf: Db) -> None:
    owner = tenant(wf)
    _, [item] = answers(wf, owner, observe(wf, owner, queue=False))
    decide_as(wf, owner, item, "redraft", comment="Say which branch.")
    drafted(wf, item, answer="Yes, on main.")
    decide_as(
        wf,
        owner,
        item,
        "redraft",
        version=item_row(wf, item).version,
        comment="Name the number of reviews.",
    )
    # The clock was stepped back between the two: the later instruction has the earlier time.
    sql(
        wf,
        "UPDATE review_decisions SET decided_at = now() - interval '1 hour' WHERE comment = :c",
        c="Name the number of reviews.",
    )

    lease = take(wf, item)

    assert lease.resume == {"decision": "redraft", "instruction": "Name the number of reviews."}


def test_keep_restores_what_the_first_reopening_recorded_whatever_its_time(wf: Db) -> None:
    owner, item = reopened(wf, "edit", answer="Yes. Two reviews are required.")
    observe(wf, owner, dict(PROTECTED))
    examine_all(wf)
    # The first reopening carries the later time. It recorded "edited"; the second, made
    # while the answer was already waiting, recorded nothing to put back.
    sql(
        wf,
        "UPDATE answer_reopenings SET reopened_at = reopened_at + interval '1 hour' "
        "WHERE was_review IS NOT NULL",
    )

    now_decide(wf, owner, item, "keep")

    assert item_row(wf, item).review == "edited"


def test_a_second_reopening_stamped_before_the_first_is_still_shown(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)
    observe(wf, owner, dict(PROTECTED))
    examine_all(wf)
    sql(
        wf,
        "UPDATE answer_reopenings SET reopened_at = reopened_at - interval '1 hour' "
        "WHERE was_review IS NULL",
    )

    found = reopening(wf, item)

    assert found is not None
    [setting] = found.settings
    # The setting changed and changed back. Without the second reopening the reviewer would
    # be shown a difference that is no longer there.
    assert setting.changes == []
    assert found.was_review == "approved"


def changed_decision(api: TestClient, db: Db) -> tuple[str, str]:
    """A run with a file, then one more decision. Returns the run and the first file's id."""
    run_id = decided_run(api, db)
    first = api.post(f"/runs/{run_id}/file").json()["id"]
    third = api.get(f"/runs/{run_id}/questions").json()["items"][2]

    async def answer_it_after_all() -> None:
        async with db.sessions() as session:
            await session.execute(
                update(RunQuestion)
                .where(RunQuestion.id == uuid.UUID(third["id"]))
                .values(review="pending", version=RunQuestion.version + 1)
            )
            await session.commit()

    db.run(answer_it_after_all())
    now = api.get(f"/run-questions/{third['id']}").json()
    assert decide(api, now, "edit", answer="Yes, every quarter.").status_code == 200
    return run_id, first


def test_a_file_is_written_from_the_decision_stored_last(api: TestClient, db: Db) -> None:
    run_id, _ = changed_decision(api, db)
    # By the clock, the rejection now looks like the newer decision.
    sql(
        db,
        "UPDATE review_decisions SET decided_at = now() - interval '1 hour' "
        "WHERE final_answer = 'Yes, every quarter.'",
    )

    made = api.post(f"/runs/{run_id}/file")

    # With the rejection taken for the newest decision, the edited wording was "not what a
    # decision recorded" and the file was refused.
    assert made.status_code == 201
    assert made.json()["current"] is True


def test_the_current_file_is_the_one_stored_last(api: TestClient, db: Db) -> None:
    run_id, first = changed_decision(api, db)
    second = api.post(f"/runs/{run_id}/file").json()["id"]
    sql(
        db,
        "UPDATE filled_files SET created_at = now() - interval '1 hour' WHERE id = :id",
        id=uuid.UUID(second),
    )

    latest = api.get(f"/runs/{run_id}/file/preview").json()["latest"]

    assert second != first
    assert (latest["id"], latest["current"]) == (second, True)


def test_a_reviewer_sees_the_check_stored_last(api: TestClient, db: Db) -> None:
    wording = "Yes, for all staff and contractors."
    _, [first, *_] = run_with_drafts(api, db, answer=wording)
    add_check(db, first["id"], "An earlier wording.", outcome="agrees")
    add_check(db, first["id"], wording)
    sql(
        db,
        "UPDATE draft_checks SET checked_at = now() - interval '1 hour' WHERE answer_checked = :a",
        a=wording,
    )

    check = api.get(f"/run-questions/{first['id']}").json()["check"]

    assert (check["outcome"], check["on_current_answer"]) == ("disagrees", True)


def test_evidence_is_paged_in_stored_order_with_no_row_skipped_or_repeated(
    make_api: ClientFactory, db: Db
) -> None:
    api = make_api(backends=backends(ORG_2FA), evidence_max_age_seconds=0)
    connect(api)
    ids = [run(api, "github.org_two_factor", "acme").json()["id"] for _ in range(4)]
    # The clock ran backwards the whole time, and two looks share a time.
    sql(
        db,
        "UPDATE evidence SET observed_at = timestamptz '2026-11-01 09:00+00' "
        "- (seq / 2) * interval '1 minute'",
    )

    pages, before = [], None
    for _ in range(4):
        params = {"limit": 1} if before is None else {"limit": 1, "before": before}
        page = api.get("/evidence", params=params).json()
        pages.extend(e["id"] for e in page["items"])
        before = page["next_before"]
        if before is None:
            break
        # The cursor is the last row's own id. The number behind the order is one sequence
        # for every tenant, so showing it would tell a tenant how busy the others are.
        assert before == pages[-1]

    assert pages == ids[::-1]
    # A cursor that is not a row of this tenant's finds nothing, like a row that is not there.
    # Shown with rows of its own on both sides of that cursor: with the cursor's number
    # looked up across tenants, the older of them would be listed.
    other = make_api(
        dev_tenant_name="another-company",
        backends=backends(ORG_2FA),
        evidence_max_age_seconds=0,
        github_orgs="another-org",
    )
    connect(other, scope="another-org")
    theirs = [run(other, "github.org_two_factor", "another-org").json()["id"] for _ in range(2)]
    newest_of_ours = run(api, "github.org_two_factor", "acme").json()["id"]
    assert len(other.get("/evidence").json()["items"]) == len(theirs)
    assert other.get("/evidence", params={"before": newest_of_ours}).json()["items"] == []
    assert api.get("/evidence", params={"before": str(uuid.uuid4())}).json()["items"] == []
    assert api.get("/evidence", params={"before": "7"}).status_code == 422


def test_a_look_asked_to_be_fresh_is_never_an_earlier_one(wf: Db) -> None:
    owner = tenant(wf)

    async def connected() -> None:
        async with wf.sessions() as session:
            session.add(Connection(tenant_id=owner, system="github", scope="acme"))
            await session.commit()

    wf.run(connected())
    first = look(wf, owner)
    # The clock steps back after the look, so the look is stamped later than "now".
    sql(wf, "UPDATE evidence SET observed_at = now() + interval '1 hour'")

    fresh = look(wf, owner, max_age=dt.timedelta(0), trigger="webhook")

    assert fresh.id != first.id


def test_the_observation_stored_first_is_examined_first(wf: Db) -> None:
    owner = tenant(wf)
    older = observe(wf, owner, scope="acme/older")
    newer = observe(wf, owner)
    sql(
        wf,
        "UPDATE drift_queue SET queued_at = now() + interval '1 hour' WHERE evidence_id = :id",
        id=older,
    )

    async def one() -> None:
        async with wf.database.tenant(owner)() as session:
            await examine.examine_next(session)

    wf.run(one())

    assert [row.evidence_id for row in all_rows(wf, DriftQueue)] == [newer]


def test_customers_a_file_went_to_are_listed_in_the_order_recorded(wf: Db) -> None:
    owner, item, _ = approved_answer(wf)
    record_sent(wf, item, "Acme Corp", "Yes, two reviews.")
    record_sent(wf, item, "Globex", "Yes, two reviews.")
    sql(
        wf, "UPDATE sent_files SET sent_at = now() + interval '1 hour' WHERE customer = 'Acme Corp'"
    )
    observe(wf, owner, OPEN)
    examine_all(wf)

    found = reopening(wf, item)

    assert found is not None
    assert [sent.customer for sent in found.sent_to] == ["Acme Corp", "Globex"]


def test_sent_records_of_a_run_are_listed_in_the_order_recorded(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    for customer in ("Acme Corp", "Globex"):
        api.post(f"/runs/{run_id}/file/{file_id}/sent", json={"customer": customer})
    sql(
        db, "UPDATE sent_files SET sent_at = now() + interval '1 hour' WHERE customer = 'Acme Corp'"
    )

    sent = api.get(f"/runs/{run_id}/file/preview").json()["sent"]

    assert [s["customer"] for s in sent] == ["Acme Corp", "Globex"]


def test_a_question_cannot_be_marked_reopened_without_its_first_reopening(wf: Db) -> None:
    import pytest
    from sqlalchemy.exc import DBAPIError

    _, item, _ = approved_answer(wf)

    # What code from before the sequence would do: set the time and nothing else. "Keep"
    # would then find no reopening and approve the agent's draft over a reviewer's edit.
    with pytest.raises(DBAPIError, match="ck_run_questions_reopened_together"):
        sql(wf, "UPDATE run_questions SET reopened_at = now() WHERE id = :id", id=item)


def redrafted_by_the_worker(
    db: Db, owner: uuid.UUID, item: uuid.UUID, answer: str, citations: list[str]
) -> None:
    """A new draft saved as the worker saves it: signed in as the application, for this
    tenant, so that what the earlier draft rested on is this tenant's to replace."""
    from standingproof.agent.types import AnswerDraft, Usage
    from standingproof.workflow import results
    from standingproof.workflow.state import Outcome

    outcome = Outcome(
        status="ready",
        path="drafted",
        reason=None,
        draft=AnswerDraft(status="answered", answer=answer, citations=citations, note=""),
        steps=2,
        cost_usd=0.02,
        usage=Usage(),
    )
    assert take(db, item) is not None

    async def save() -> bool:
        async with db.database.tenant(owner)() as session:
            return await results.save_result(session, item_id=item, owner="w1", outcome=outcome)

    assert db.run(save())


def test_the_same_change_reopening_an_answer_again_starts_a_new_wait(wf: Db) -> None:
    owner, item, first = approved_answer(wf)
    observe(wf, owner, OPEN)
    examine_all(wf)
    # The reviewer sends it back, and the new draft cites the first observation again. The
    # newest reading is queued for it and is the same change as before.
    now_decide(wf, owner, item, "redraft", comment="Say which branch.")
    redrafted_by_the_worker(wf, owner, item, "Yes, on main.", [f"evidence:{first}"])
    examine_all(wf)
    row = item_row(wf, item)
    assert (row.review, row.reopened_at is not None) == ("pending", True)

    # The earlier reopening belonged to a wait a decision closed. It recorded "approved",
    # and there is no approved wording any more: keeping must approve this draft, not fail.
    now_decide(wf, owner, item, "keep")

    kept = item_row(wf, item)
    assert (kept.review, kept.final_answer) == ("approved", "Yes, on main.")


def test_a_new_wait_starts_after_every_earlier_reopening_of_the_answer(wf: Db) -> None:
    from tests.drift.test_view import _also_cites

    other = "github.secret_scanning"
    owner, item, first = approved_answer(wf)
    scanning = observe(wf, owner, {"secret_scanning_enabled": True}, check=other, queue=False)
    wf.run(_also_cites(wf, owner, item, [first, scanning]))
    # First wait: branch protection changes. Kept.
    observe(wf, owner, OPEN)
    examine_all(wf)
    now_decide(wf, owner, item, "keep")
    # Second wait: secret scanning changes. Sent back for a redraft, which closes it.
    off = observe(wf, owner, {"secret_scanning_enabled": False}, check=other)
    examine_all(wf)
    now_decide(wf, owner, item, "redraft", comment="Say it is off.")
    # The redraft cites the first look at branch protection again, so the change of the
    # first wait reopens the answer a second time.
    redrafted_by_the_worker(
        wf, owner, item, "No, it is off.", [f"evidence:{first}", f"evidence:{off}"]
    )
    examine_all(wf)
    assert item_row(wf, item).reopened_at is not None

    # Neither earlier reopening belongs to this wait. Both recorded "approved", and there
    # is no approved wording now: keeping approves this draft.
    now_decide(wf, owner, item, "keep")

    kept = item_row(wf, item)
    assert (kept.review, kept.final_answer) == ("approved", "No, it is off.")
