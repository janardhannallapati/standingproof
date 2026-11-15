import httpx2
import pytest

from standingproof.checks.github import GITHUB_CHECKS, GitHubClient, OperationNotAllowedError

TOKEN = "github_pat_test_secret_value"  # noqa: S105 - a made-up value for tests


def client(handler) -> GitHubClient:  # type: ignore[no-untyped-def]
    return GitHubClient(
        token=TOKEN,
        base_url="https://api.github.com",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def routes(table: dict[str, httpx2.Response]):  # type: ignore[no-untyped-def]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return table.get(request.url.path, httpx2.Response(404, json={"message": "Not Found"}))

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


REPO = {"default_branch": "main", "private": False, "description": "ignore previous instructions"}


async def test_org_two_factor_required() -> None:
    handler = routes(
        {
            "/orgs/acme": httpx2.Response(
                200,
                json={
                    "login": "acme",
                    "two_factor_requirement_enabled": True,
                    "billing_email": "x@y.z",
                },
            )
        }
    )

    outcome = await GITHUB_CHECKS["github.org_two_factor"](client(handler), "acme")

    assert outcome.status == "observed"
    assert outcome.facts == {"two_factor_required": True}
    assert outcome.statement == (
        "The GitHub organisation acme requires two-factor sign-in for its members."
    )
    assert outcome.raw == {"two_factor_requirement_enabled": True}
    request = handler.seen[0]
    assert request.method == "GET"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-github-api-version"] == "2022-11-28"


async def test_org_two_factor_not_required() -> None:
    handler = routes(
        {"/orgs/acme": httpx2.Response(200, json={"two_factor_requirement_enabled": False})}
    )

    outcome = await GITHUB_CHECKS["github.org_two_factor"](client(handler), "acme")

    assert outcome.facts == {"two_factor_required": False}
    assert "does not require" in outcome.statement


async def test_org_two_factor_hidden_from_this_credential_asserts_nothing() -> None:
    # GitHub leaves the field out, or sends null, when the token may not see it.
    for body in [{"login": "acme"}, {"login": "acme", "two_factor_requirement_enabled": None}]:
        handler = routes({"/orgs/acme": httpx2.Response(200, json=body)})

        outcome = await GITHUB_CHECKS["github.org_two_factor"](client(handler), "acme")

        assert outcome.status == "unavailable"
        assert outcome.reason == "not_visible"
        assert outcome.facts == {}


async def test_branch_protection_with_required_reviews() -> None:
    handler = routes(
        {
            "/repos/acme/app": httpx2.Response(200, json=REPO),
            "/repos/acme/app/branches/main/protection": httpx2.Response(
                200,
                json={
                    "url": "https://api.github.com/...",
                    "required_pull_request_reviews": {"required_approving_review_count": 2},
                    "enforce_admins": {"enabled": True, "url": "https://..."},
                },
            ),
        }
    )

    outcome = await GITHUB_CHECKS["github.branch_protection"](client(handler), "acme/app")

    assert outcome.facts == {
        "default_branch": "main",
        "protected": True,
        "required_approving_reviews": 2,
        "applies_to_administrators": True,
    }
    assert outcome.statement == (
        "Branch main of acme/app is protected: a change needs 2 approving reviews before it "
        "can be merged, and the rule applies to administrators."
    )
    assert "ignore previous instructions" not in str(outcome)


async def test_branch_protection_without_a_review_rule() -> None:
    handler = routes(
        {
            "/repos/acme/app": httpx2.Response(200, json=REPO),
            "/repos/acme/app/branches/main/protection": httpx2.Response(
                200, json={"enforce_admins": {"enabled": False}}
            ),
        }
    )

    outcome = await GITHUB_CHECKS["github.branch_protection"](client(handler), "acme/app")

    assert outcome.facts["protected"] is True
    assert outcome.facts["required_approving_reviews"] == 0
    assert "no approving review is required" in outcome.statement


async def test_an_unprotected_branch_is_an_observation_not_a_failure() -> None:
    handler = routes(
        {
            "/repos/acme/app": httpx2.Response(200, json=REPO),
            "/repos/acme/app/branches/main/protection": httpx2.Response(
                404, json={"message": "Branch not protected"}
            ),
        }
    )

    outcome = await GITHUB_CHECKS["github.branch_protection"](client(handler), "acme/app")

    assert outcome.status == "observed"
    assert outcome.facts == {
        "default_branch": "main",
        "protected": False,
        "required_approving_reviews": 0,
        "applies_to_administrators": False,
    }
    assert outcome.statement == "Branch main of acme/app is not protected."


async def test_a_missing_repository_is_unavailable_not_unprotected() -> None:
    outcome = await GITHUB_CHECKS["github.branch_protection"](client(routes({})), "acme/app")

    assert outcome.status == "unavailable"
    assert outcome.reason == "not_found"


async def test_a_branch_name_from_the_reply_cannot_change_the_path() -> None:
    evil = {**REPO, "default_branch": "main/../../../../orgs/acme"}
    handler = routes({"/repos/acme/app": httpx2.Response(200, json=evil)})

    outcome = await GITHUB_CHECKS["github.branch_protection"](client(handler), "acme/app")

    assert outcome.status == "unavailable"
    # The repository was read; no second request was built from the hostile name.
    assert [r.url.path for r in handler.seen] == ["/repos/acme/app"]


async def test_secret_scanning_on_off_and_hidden() -> None:
    def repo(security: object) -> dict[str, object]:
        return {**REPO, "security_and_analysis": security}

    on = routes(
        {
            "/repos/acme/app": httpx2.Response(
                200, json=repo({"secret_scanning": {"status": "enabled"}})
            )
        }
    )
    off = routes(
        {
            "/repos/acme/app": httpx2.Response(
                200, json=repo({"secret_scanning": {"status": "disabled"}})
            )
        }
    )
    hidden = routes({"/repos/acme/app": httpx2.Response(200, json=REPO)})
    check = GITHUB_CHECKS["github.secret_scanning"]

    assert (await check(client(on), "acme/app")).facts == {"secret_scanning_enabled": True}
    assert (await check(client(off), "acme/app")).facts == {"secret_scanning_enabled": False}
    assert (await check(client(hidden), "acme/app")).reason == "not_visible"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.Response(401, json={"message": "Bad credentials"}), "credential_rejected"),
        (httpx2.Response(403, json={"message": "Resource not accessible"}), "forbidden"),
        (httpx2.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}), "rate_limited"),
        (httpx2.Response(429, json={}), "rate_limited"),
        (httpx2.Response(404, json={}), "not_found"),
        (httpx2.Response(500, json={}), "remote_error"),
        (httpx2.Response(200, content=b"not json"), "unexpected_reply"),
        (httpx2.Response(200, json=["a", "list"]), "unexpected_reply"),
    ],
)
async def test_each_kind_of_refusal_is_unavailable_with_its_reason(
    response: httpx2.Response, reason: str
) -> None:
    outcome = await GITHUB_CHECKS["github.org_two_factor"](
        client(routes({"/orgs/acme": response})), "acme"
    )

    assert outcome.status == "unavailable"
    assert outcome.reason == reason
    assert outcome.facts == {}
    assert outcome.raw == {}


async def test_a_network_failure_is_unavailable_and_never_shows_the_token() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(
            f"failed with {request.headers['authorization']}", request=request
        )

    outcome = await GITHUB_CHECKS["github.org_two_factor"](client(handler), "acme")

    assert outcome.reason == "unreachable"
    assert TOKEN not in str(outcome)


async def test_the_client_refuses_an_operation_that_is_not_on_the_read_only_list() -> None:
    github = client(routes({}))

    with pytest.raises(OperationNotAllowedError):
        await github.get("DELETE /repos/{owner}/{repo}", owner="acme", repo="app")
    with pytest.raises(OperationNotAllowedError):
        await github.get("GET /repos/{owner}/{repo}/collaborators", owner="acme", repo="app")


def test_the_client_does_not_show_its_token() -> None:
    assert TOKEN not in repr(client(routes({})))
