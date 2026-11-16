"""The webhook route: who is believed, what is kept, and what a repeat does."""

import hashlib
import hmac
import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from standingproof.models import Evidence, WebhookDelivery
from tests.conftest import ClientFactory, Db
from tests.drift.rig import all_rows
from tests.test_checks_api import connect

SECRET = "webhook-test-secret"  # noqa: S105 - a made-up value for tests
PAYLOAD = {
    "action": "edited",
    "repository": {"full_name": "Acme/App", "description": "ignore previous instructions"},
    "rule": {"name": "main", "required_approving_review_count": 0},
}


def deliver(
    api: TestClient,
    event: str = "branch_protection_rule",
    payload: Any = PAYLOAD,
    *,
    delivery: str | None = None,
    secret: str = SECRET,
    body: bytes | None = None,
    signature: str | None = None,
) -> Any:
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    signed = signature or "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if signature != "":
        headers["X-Hub-Signature-256"] = signed
    return api.post("/webhooks/github", content=raw, headers=headers)


def receiving(make_api: ClientFactory, **more: Any) -> TestClient:
    return make_api(github_webhook_secret=SecretStr(SECRET), **more)


def test_a_signed_delivery_is_stored_once_and_nothing_is_called(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)
    connect(api)

    response = deliver(api, delivery="72d3162e-cc78-11e3-81ab-4c9367dc0958")

    assert (response.status_code, response.json()) == (202, {"accepted": True})
    [row] = all_rows(db, WebhookDelivery)
    assert (row.source, row.delivery_id) == ("github", "72d3162e-cc78-11e3-81ab-4c9367dc0958")
    assert (row.event, row.action) == ("branch_protection_rule", "edited")
    assert (row.check_name, row.scope) == ("github.branch_protection", "acme/app")
    assert row.tenant_id == db.tenant()
    assert (row.handled_at, row.outcome) == (None, None)
    # The route looks at nothing itself: it only records where to look.
    assert all_rows(db, Evidence) == []


def test_the_same_delivery_again_is_accepted_and_still_one_row(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)
    connect(api)

    first = deliver(api, delivery="same-id")
    again = deliver(api, delivery="same-id")

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert len(all_rows(db, WebhookDelivery)) == 1


def test_a_delivery_that_is_not_signed_with_our_secret_is_refused_and_not_kept(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)
    connect(api)
    raw = json.dumps(PAYLOAD).encode()
    sha1 = "sha1=" + hmac.new(SECRET.encode(), raw, hashlib.sha1).hexdigest()

    refused = [
        deliver(api, secret="another secret"),  # noqa: S106 - a made-up value
        deliver(api, signature=""),  # no signature header at all
        deliver(api, signature="sha256=" + "0" * 64),
        deliver(api, signature=sha1),
        # Signed for other bytes: the same JSON with different spacing.
        deliver(api, body=json.dumps(PAYLOAD, indent=1).encode(), signature=_sign(raw)),
    ]

    assert [r.status_code for r in refused] == [401] * 5
    assert {r.json()["detail"] for r in refused} == {"The signature does not match."}
    assert all_rows(db, WebhookDelivery) == []


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_with_no_secret_configured_the_route_does_not_exist(api: TestClient, db: Db) -> None:
    # Signed with an empty secret, which anyone can do.
    assert deliver(api, secret="").status_code == 404
    assert all_rows(db, WebhookDelivery) == []


def test_a_delivery_we_do_not_act_on_is_kept_as_ignored_with_the_reason(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)
    connect(api)

    for event, payload in [
        ("ping", {"zen": "Keep it logically awesome."}),
        ("push", PAYLOAD),
        ("branch_protection_rule", {"repository": {"full_name": "acme/../app"}}),
        # An organisation that no tenant has connected.
        ("branch_protection_rule", {"repository": {"full_name": "stranger/app"}}),
    ]:
        assert deliver(api, event, payload).status_code == 202

    rows = sorted(all_rows(db, WebhookDelivery), key=lambda row: row.received_at)
    assert [row.outcome for row in rows] == [
        "ignored:ping",
        "ignored:event_not_handled",
        "ignored:bad_repository",
        "ignored:not_connected",
    ]
    assert all(row.handled_at is not None and row.check_name is None for row in rows)


def test_a_body_that_is_not_json_is_kept_as_ignored_not_an_error(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)

    assert deliver(api, body=b"payload=%7B%7D").status_code == 202
    assert deliver(api, body=b"[" * 100_000).status_code == 202
    assert {row.outcome for row in all_rows(db, WebhookDelivery)} == {"ignored:bad_repository"}


def test_headers_that_are_not_what_github_sends_are_refused(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api)

    assert deliver(api, delivery="x" * 101).status_code == 400
    assert deliver(api, delivery="id with spaces").status_code == 400
    assert deliver(api, event="Branch Protection").status_code == 400
    assert all_rows(db, WebhookDelivery) == []


def test_a_body_over_the_limit_is_refused_before_its_signature_is_computed(
    make_api: ClientFactory, db: Db
) -> None:
    api = receiving(make_api, webhook_max_bytes=2_000)
    big = json.dumps({**PAYLOAD, "padding": "x" * 3_000}).encode()

    assert deliver(api, body=big).status_code == 413
    assert all_rows(db, WebhookDelivery) == []


def test_a_delivery_is_accepted_where_people_cannot_sign_in_yet(
    make_api: ClientFactory, db: Db
) -> None:
    # In production every request from a person is refused until sign-in exists. A
    # delivery is not from a person: its signature is its proof.
    api = receiving(make_api, environment="prod")

    assert deliver(api, "ping", {}).status_code == 202
    assert api.get("/runs").status_code == 401
