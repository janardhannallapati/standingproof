"""From a drafted answer to a reviewer deciding it again, through every real part.

A scripted model stands in for the provider and a fake GitHub for the API. Everything
between them is the product: the worker, the check tool, review, the filled file, the sent
record, the signed webhook route, the watcher, and what the API then tells the screen.
"""

import uuid
from typing import Any

from fastapi.testclient import TestClient

from standingproof.drift.watcher import Watcher, WatcherDeps
from tests.conftest import ClientFactory, Db
from tests.drift.github_fake import REPO, CitesItsCheck, FakeGitHub
from tests.drift.test_webhook import deliver, receiving
from tests.test_checks_api import connect
from tests.test_review_api import decide
from tests.test_runs_api import start, uploaded
from tests.workflow.rig import Rig

QUESTION = "Do code changes need a second person's review?"


def question(api: TestClient, run_id: str) -> dict[str, Any]:
    [listed] = api.get(f"/runs/{run_id}/questions").json()["items"]
    return dict(api.get(f"/run-questions/{listed['id']}").json()) | {
        "listed_reopened": listed["reopened"]
    }


def test_a_setting_changes_after_an_answer_was_sent(
    make_api: ClientFactory, db: Db, redis_url: str
) -> None:
    github = FakeGitHub()
    api = receiving(make_api, backends=github.backends())
    connect(api)

    # The agent looks at branch protection and drafts from what it saw.
    run_id = start(api, uploaded(api, [QUESTION])).json()["id"]
    rig = Rig(db, redis_url, CitesItsCheck(), backends=github.backends(), github_orgs="acme")
    rig.add([uuid.UUID(question(api, run_id)["id"])])
    assert rig.work_through_the_queue() == 1, rig.given_back
    drafted = question(api, run_id)
    assert (drafted["status"], drafted["reopening"], drafted["listed_reopened"]) == (
        "ready",
        None,
        False,
    )
    assert "needs 2 approving reviews" in drafted["answer"]

    # A reviewer approves it, makes the file and records it as sent.
    assert decide(api, drafted, "approve").status_code == 200
    file_id = api.post(f"/runs/{run_id}/file", json={}).json()["id"]
    sent = api.post(f"/runs/{run_id}/file/{file_id}/sent", json={"customer": "Acme Corp"})
    assert sent.status_code == 201

    # Branch protection is switched off, and GitHub says something changed.
    github.reviews = None
    assert (
        deliver(api, payload={"action": "deleted", "repository": {"full_name": REPO}}).status_code
        == 202
    )
    announced: list[uuid.UUID] = []

    async def announce(changed: uuid.UUID) -> None:
        announced.append(changed)

    settings = api.app.state.settings.model_copy(  # type: ignore[attr-defined]
        update={"webhook_settle_seconds": 0, "webhook_min_gap_seconds": 0}
    )
    watcher = Watcher(WatcherDeps(settings, db.sessions, github.backends(), announce))
    db.run(watcher.tick())

    # The answer is back in front of the reviewer, with what changed and who has it.
    assert announced == [uuid.UUID(run_id)]
    reopened = question(api, run_id)
    assert (reopened["review"], reopened["listed_reopened"]) == ("pending", True)
    assert reopened["final_answer"] == drafted["answer"]
    change = reopened["reopening"]
    assert change["was_review"] == "approved"
    [setting] = change["settings"]
    assert (setting["check_name"], setting["scope"], setting["trigger"]) == (
        "github.branch_protection",
        REPO,
        "webhook",
    )
    assert "is protected" in setting["was"]["statement"]
    assert setting["now"]["statement"] == f"Branch main of {REPO} is not protected."
    assert {"fact": "required_approving_reviews", "was": 2, "now": 0} in setting["changes"]
    assert {"fact": "protected", "was": True, "now": False} in setting["changes"]
    assert [each["customer"] for each in change["sent_to"]] == ["Acme Corp"]
    [listed_run] = api.get("/runs").json()["items"]
    assert listed_run["reopened"] == 1

    # No file can be made, and the sent record says one of its answers was reopened.
    preview = api.get(f"/runs/{run_id}/file/preview").json()
    assert (preview["ready"], preview["undecided"]) == (False, 1)
    assert [(s["customer"], s["reopened"], s["current"]) for s in preview["sent"]] == [
        ("Acme Corp", 1, False)
    ]
    assert api.post(f"/runs/{run_id}/file", json={}).status_code == 409

    # The Changes page lists it, with the answer and the customer.
    [listed] = api.get("/changes").json()["items"]
    assert (listed["check_name"], listed["scope"], listed["trigger"]) == (
        "github.branch_protection",
        REPO,
        "webhook",
    )
    assert listed["now"]["statement"] == setting["now"]["statement"]
    assert listed["was"]["statement"] == setting["was"]["statement"]
    [answer] = listed["answers"]
    assert (answer["run_id"], answer["run_question_id"], answer["waiting"]) == (
        run_id,
        reopened["id"],
        True,
    )
    assert (answer["question"], answer["position"]) == (QUESTION, 1)
    assert [each["customer"] for each in answer["sent_to"]] == ["Acme Corp"]
    watched = api.get("/checks/watched").json()["items"]
    assert [(w["check_name"], w["scope"], w["last_status"]) for w in watched] == [
        ("github.branch_protection", REPO, "observed")
    ]

    # Approve is refused; keeping it puts everything back, and the file is current again.
    assert decide(api, reopened, "approve").status_code == 409
    kept = decide(api, reopened, "keep")
    assert kept.status_code == 200
    assert (kept.json()["review"], kept.json()["reopening"]) == ("approved", None)
    after = api.get(f"/runs/{run_id}/file/preview").json()
    assert (after["ready"], after["latest"]["id"], after["latest"]["current"]) == (
        True,
        file_id,
        True,
    )
    assert [(s["reopened"], s["current"]) for s in after["sent"]] == [(0, True)]
    assert api.get("/runs").json()["items"][0]["reopened"] == 0
    assert api.get("/changes").json()["items"][0]["answers"][0]["waiting"] is False


def test_another_tenants_changes_are_not_listed(make_api: ClientFactory, db: Db) -> None:
    from tests.drift.rig import OPEN, approved_answer, examine_all, observe

    api = receiving(make_api)
    owner, _, _ = approved_answer(db, "someone-else")
    observe(db, owner, OPEN)
    examine_all(db)

    assert api.get("/changes").json() == {"items": []}
    assert api.get("/checks/watched").json() == {"items": []}
