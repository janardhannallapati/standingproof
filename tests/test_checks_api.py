"""Running read-only checks through the API and keeping what they observed."""

import asyncio
import time
import uuid
from typing import Any

import httpx2
from botocore.exceptions import NoCredentialsError
from fastapi.testclient import TestClient

from standingproof.checks.aws import AwsReader
from standingproof.checks.service import CheckBackends
from tests.checks.test_github import client as github_client
from tests.checks.test_github import routes
from tests.conftest import ClientFactory
from tests.test_library_api import count

ORG_2FA = {"/orgs/acme": httpx2.Response(200, json={"two_factor_requirement_enabled": True})}


def backends(table: dict[str, httpx2.Response] | None = None, handler: Any = None) -> CheckBackends:
    handler = handler or routes(table or {})
    return CheckBackends(github=github_client(handler), aws=None)


def connect(api: TestClient, system: str = "github", scope: str = "acme") -> httpx2.Response:
    return api.post("/connections", json={"system": system, "scope": scope})


def run(api: TestClient, name: str, scope: str) -> httpx2.Response:
    return api.post(f"/checks/{name}/run", json={"scope": scope})


def test_the_registry_is_listed(api: TestClient) -> None:
    listed = api.get("/checks").json()["checks"]

    assert len(listed) == 7
    assert {"name", "system", "scope_kind", "description"} <= set(listed[0])
    assert "github.branch_protection" in {c["name"] for c in listed}


def test_connecting_a_scope_twice_is_one_connection(api: TestClient) -> None:
    first = connect(api)
    second = connect(api, scope="ACME")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert [c["scope"] for c in api.get("/connections").json()["connections"]] == ["acme"]


def test_a_connection_must_name_a_system_and_a_scope_of_the_right_shape(api: TestClient) -> None:
    assert connect(api, system="gitlab").status_code == 422
    assert connect(api, scope="acme/app").status_code == 422  # a repository is not connected
    assert connect(api, system="aws", scope="12345").status_code == 422
    assert connect(api, system="aws", scope="123456789012").status_code == 201


def test_a_check_on_a_connected_scope_stores_and_returns_evidence(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    api = make_api(backends=backends(ORG_2FA))
    connect(api)

    response = run(api, "github.org_two_factor", "acme")

    assert response.status_code == 200
    evidence = response.json()
    assert evidence["status"] == "observed"
    assert evidence["check_name"] == "github.org_two_factor"
    assert evidence["scope"] == "acme"
    assert evidence["facts"] == {"two_factor_required": True}
    assert evidence["raw_result"] == {"two_factor_requirement_enabled": True}
    assert evidence["statement"].startswith("The GitHub organisation acme requires")
    assert evidence["observed_at"].endswith(("Z", "+00:00"))
    assert api.get(f"/evidence/{evidence['id']}").json() == evidence
    assert count(migrated_postgres_url, "evidence") == 1


def test_an_unknown_check_is_refused_before_any_call_and_the_attempt_is_recorded(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    handler = routes(ORG_2FA)
    api = make_api(backends=backends(handler=handler))
    connect(api)

    response = run(api, "github.delete_everything", "acme")

    assert response.status_code == 404
    assert handler.seen == []
    assert count(migrated_postgres_url, "evidence WHERE status = 'denied'") == 1


def test_a_scope_that_is_not_connected_is_refused_before_any_call(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    handler = routes(ORG_2FA)
    api = make_api(backends=backends(handler=handler))
    connect(api, scope="another-org")

    not_connected = run(api, "github.org_two_factor", "acme")
    wrong_shape = run(api, "github.org_two_factor", "acme/../../user")

    assert not_connected.status_code == 403
    assert "scope_not_connected" in not_connected.json()["detail"]
    assert wrong_shape.status_code == 403
    assert handler.seen == []
    assert count(migrated_postgres_url, "evidence WHERE status = 'denied'") == 2


def test_a_refusal_by_the_remote_system_is_stored_and_asserts_nothing(
    make_api: ClientFactory,
) -> None:
    api = make_api(backends=backends({"/orgs/acme": httpx2.Response(403, json={})}))
    connect(api)

    evidence = run(api, "github.org_two_factor", "acme").json()

    assert evidence["status"] == "unavailable"
    assert evidence["reason"] == "forbidden"
    assert evidence["facts"] == {}
    assert evidence["statement"].startswith("Not observed")


def test_without_a_credential_the_check_reports_not_configured(api: TestClient) -> None:
    connect(api)
    connect(api, system="aws", scope="123456789012")

    github = run(api, "github.org_two_factor", "acme").json()
    aws = run(api, "aws.root_mfa", "123456789012").json()

    assert github["status"] == "not_configured"
    assert aws["status"] == "not_configured"


def test_fresh_evidence_is_reused_and_stale_evidence_is_checked_again(
    make_api: ClientFactory,
) -> None:
    handler = routes(ORG_2FA)
    fresh = make_api(backends=backends(handler=handler))
    connect(fresh)

    first = run(fresh, "github.org_two_factor", "acme").json()
    again = run(fresh, "github.org_two_factor", "ACME").json()

    assert again["id"] == first["id"]
    assert len(handler.seen) == 1

    impatient = make_api(backends=backends(handler=handler), evidence_max_age_seconds=0)
    third = run(impatient, "github.org_two_factor", "acme").json()

    assert third["id"] != first["id"]
    assert len(handler.seen) == 2


def test_an_unavailable_result_is_never_reused(make_api: ClientFactory) -> None:
    handler = routes({"/orgs/acme": httpx2.Response(500, json={})})
    api = make_api(backends=backends(handler=handler))
    connect(api)

    run(api, "github.org_two_factor", "acme")
    run(api, "github.org_two_factor", "acme")

    assert len(handler.seen) == 2


def test_a_check_that_takes_too_long_is_stored_as_unavailable(make_api: ClientFactory) -> None:
    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json={})

    api = make_api(backends=backends(handler=slow), check_timeout_seconds=0.2)
    connect(api)

    started = time.perf_counter()
    evidence = run(api, "github.org_two_factor", "acme").json()

    assert time.perf_counter() - started < 3
    assert evidence["status"] == "unavailable"
    assert evidence["reason"] == "timed_out"


def test_an_aws_check_runs_through_the_same_path(make_api: ClientFactory) -> None:
    from tests.checks.test_aws import ACCOUNT, Stubbed

    stubbed = Stubbed()
    stubbed.identity()
    stubbed.stubs["iam"].add_response(
        "get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1}}
    )
    api = make_api(backends=CheckBackends(github=None, aws=stubbed.reader()))
    connect(api, system="aws", scope=ACCOUNT)

    evidence = run(api, "aws.root_mfa", ACCOUNT).json()

    assert evidence["status"] == "observed"
    assert evidence["facts"] == {"root_mfa_enabled": True}


def test_evidence_is_listed_newest_first_filtered_and_paged(make_api: ClientFactory) -> None:
    api = make_api(backends=backends(ORG_2FA), evidence_max_age_seconds=0)
    connect(api)
    ids = [run(api, "github.org_two_factor", "acme").json()["id"] for _ in range(3)]
    run(api, "github.secret_scanning", "acme/app")

    first = api.get("/evidence", params={"check": "github.org_two_factor", "limit": 2}).json()
    second = api.get(
        "/evidence",
        params={"check": "github.org_two_factor", "limit": 2, "before": first["next_before"]},
    ).json()

    assert [e["id"] for e in first["items"]] == ids[:0:-1]
    assert [e["id"] for e in second["items"]] == ids[:1]
    assert second["next_before"] is None
    assert len(api.get("/evidence", params={"scope": "acme/app"}).json()["items"]) == 1


def test_another_tenant_sees_none_of_it(make_api: ClientFactory) -> None:
    ours = make_api(backends=backends(ORG_2FA))
    theirs = make_api(dev_tenant_name="another-company", backends=backends(ORG_2FA))
    connect(ours)
    evidence_id = run(ours, "github.org_two_factor", "acme").json()["id"]

    assert theirs.get(f"/evidence/{evidence_id}").status_code == 404
    assert theirs.get(f"/evidence/{uuid.uuid4()}").status_code == 404
    assert theirs.get("/evidence").json()["items"] == []
    assert theirs.get("/connections").json()["connections"] == []
    # Our connection does not let them check our organisation.
    assert run(theirs, "github.org_two_factor", "acme").status_code == 403


def test_the_default_backends_never_reach_for_real_credentials() -> None:
    # Guards the test suite itself: a developer's own AWS keys must never be used by a test.
    from tests.conftest import no_backends

    reader = no_backends().aws
    assert isinstance(reader, AwsReader)
    try:
        reader._client("sts")
    except NoCredentialsError:
        return
    raise AssertionError("the test AWS reader built a real client")


def test_a_scope_the_deployment_has_not_declared_cannot_be_connected(
    make_api: ClientFactory, migrated_postgres_url: str
) -> None:
    # The credentials belong to the deployment, not to a tenant. So which organisations and
    # accounts they may be used on is the operator's decision, made in configuration.
    api = make_api(github_orgs="acme", aws_accounts="")

    undeclared_org = connect(api, scope="someone-elses-org")
    undeclared_account = connect(api, system="aws", scope="123456789012")

    assert undeclared_org.status_code == 403
    assert undeclared_account.status_code == 403
    assert connect(api, scope="ACME").status_code == 201
    assert count(migrated_postgres_url, "connections") == 1


def test_by_default_no_scope_is_declared_so_nothing_can_be_connected(
    make_api: ClientFactory,
) -> None:
    api = make_api(github_orgs="", aws_accounts="")

    assert connect(api).status_code == 403


def test_a_scope_connected_by_one_tenant_cannot_be_connected_by_another(
    make_api: ClientFactory,
) -> None:
    ours = make_api()
    theirs = make_api(dev_tenant_name="another-company")

    assert connect(ours).status_code == 201
    taken = connect(theirs)

    assert taken.status_code == 409
    assert theirs.get("/connections").json()["connections"] == []
    assert run(theirs, "github.org_two_factor", "acme").status_code == 403
    # The tenant that holds it is unaffected, and connecting again is still a no-op.
    assert connect(ours).status_code == 200


def test_a_connection_stops_working_when_the_operator_withdraws_the_scope(
    make_api: ClientFactory,
) -> None:
    handler = routes(ORG_2FA)
    connect(make_api())  # connected while "acme" was declared

    later = make_api(backends=backends(handler=handler), github_orgs="")
    response = run(later, "github.org_two_factor", "acme")

    assert response.status_code == 403
    assert handler.seen == []
