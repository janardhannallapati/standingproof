"""Read-only checks against the GitHub REST API.

The client can only send GET requests for the operations on the read-only list. Each check
turns one or two replies into a few facts and one sentence; nothing else from the reply is
kept, and text from the reply (a description, a name) never reaches the sentence.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from standingproof.checks.outcome import CheckOutcome, observed, unavailable
from standingproof.checks.redact import keep
from standingproof.checks.registry import (
    GET_BRANCH_PROTECTION,
    GET_ORG,
    GET_REPO,
    READ_ONLY_OPERATIONS,
)

_API_VERSION = "2022-11-28"
# A branch name comes from GitHub's reply and goes into the next request's path.
_BRANCH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_/-]|\.(?!\.))*")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class OperationNotAllowedError(Exception):
    """Code asked for an API operation that is not on the read-only list. A bug, not a
    condition to handle: it is raised, not turned into an outcome."""


class _Refused(Exception):
    """The request did not produce a usable reply."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class GitHubClient:
    def __init__(
        self, *, token: str, base_url: str, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, connect=5.0))

    def __repr__(self) -> str:
        return f"GitHubClient(base_url={self._base_url!r}, token=<hidden>)"

    async def get(self, operation: str, **params: str) -> tuple[int, dict[str, Any]]:
        """Send one allow-listed GET. Returns the status and the JSON object of the reply.

        `operation` is a constant from the registry, such as "GET /orgs/{org}". The path is
        built from that template, so no caller can reach an endpoint that is not listed.
        """
        if operation not in READ_ONLY_OPERATIONS or not operation.startswith("GET "):
            raise OperationNotAllowedError(operation)
        path = _PLACEHOLDER.sub(lambda match: params[match.group(1)], operation[4:])
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
            )
        except httpx2.HTTPError as error:
            # `from None`: the original exception holds the request, and with it the token.
            raise _Refused(
                "unreachable", f"GitHub could not be reached ({type(error).__name__})."
            ) from None
        status = response.status_code
        if status == httpx2.codes.TOO_MANY_REQUESTS or (
            status == httpx2.codes.FORBIDDEN
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise _Refused("rate_limited", "GitHub's rate limit was reached.")
        if status == httpx2.codes.UNAUTHORIZED:
            raise _Refused("credential_rejected", "GitHub rejected the credential.")
        if status == httpx2.codes.FORBIDDEN:
            raise _Refused("forbidden", "the credential may not read this.")
        if status >= httpx2.codes.INTERNAL_SERVER_ERROR:
            raise _Refused("remote_error", f"GitHub answered {status}.")
        try:
            body = response.json()
        except ValueError:
            body = None
        if status == httpx2.codes.NOT_FOUND:
            return status, body if isinstance(body, dict) else {}
        if status != httpx2.codes.OK or not isinstance(body, dict):
            raise _Refused("unexpected_reply", "GitHub's reply was not in the expected form.")
        return status, body

    async def aclose(self) -> None:
        await self._client.aclose()


def _not_found(what: str) -> _Refused:
    return _Refused("not_found", f"{what} was not found, or the credential cannot see it.")


async def org_two_factor(github: GitHubClient, scope: str) -> CheckOutcome:
    status, org = await github.get(GET_ORG, org=scope)
    if status == httpx2.codes.NOT_FOUND:
        raise _not_found(f"the organisation {scope}")
    required = org.get("two_factor_requirement_enabled")
    if not isinstance(required, bool):
        # GitHub omits the field, or sends null, for a credential that may not see it.
        raise _Refused(
            "not_visible", "this credential cannot see the organisation's two-factor setting."
        )
    verb = "requires" if required else "does not require"
    return observed(
        f"The GitHub organisation {scope} {verb} two-factor sign-in for its members.",
        {"two_factor_required": required},
        keep(org, ["two_factor_requirement_enabled"]),
    )


async def branch_protection(github: GitHubClient, scope: str) -> CheckOutcome:
    owner, repo = scope.split("/")
    status, repository = await github.get(GET_REPO, owner=owner, repo=repo)
    if status == httpx2.codes.NOT_FOUND:
        raise _not_found(f"the repository {scope}")
    branch = repository.get("default_branch")
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch):
        raise _Refused("unexpected_reply", "the repository's default branch name is unusable.")

    status, protection = await github.get(
        GET_BRANCH_PROTECTION, owner=owner, repo=repo, branch=branch
    )
    if status == httpx2.codes.NOT_FOUND:
        # For this endpoint, "not found" on a repository we just read means "not protected".
        return observed(
            f"Branch {branch} of {scope} is not protected.",
            {
                "default_branch": branch,
                "protected": False,
                "required_approving_reviews": 0,
                "applies_to_administrators": False,
            },
            {},
        )
    raw = keep(
        protection,
        ["required_pull_request_reviews.required_approving_review_count", "enforce_admins.enabled"],
    )
    reviews = raw.get("required_pull_request_reviews", {}).get("required_approving_review_count", 0)
    admins = raw.get("enforce_admins", {}).get("enabled", False)
    if not isinstance(reviews, int) or not isinstance(admins, bool):
        raise _Refused("unexpected_reply", "GitHub's reply was not in the expected form.")
    if reviews:
        plural = "review" if reviews == 1 else "reviews"
        rule = f"a change needs {reviews} approving {plural} before it can be merged"
    else:
        rule = "no approving review is required"
    whom = "applies to administrators" if admins else "does not apply to administrators"
    return observed(
        f"Branch {branch} of {scope} is protected: {rule}, and the rule {whom}.",
        {
            "default_branch": branch,
            "protected": True,
            "required_approving_reviews": reviews,
            "applies_to_administrators": admins,
        },
        raw,
    )


async def secret_scanning(github: GitHubClient, scope: str) -> CheckOutcome:
    owner, repo = scope.split("/")
    status, repository = await github.get(GET_REPO, owner=owner, repo=repo)
    if status == httpx2.codes.NOT_FOUND:
        raise _not_found(f"the repository {scope}")
    raw = keep(repository, ["security_and_analysis.secret_scanning.status"])
    state = raw.get("security_and_analysis", {}).get("secret_scanning", {}).get("status")
    if state not in ("enabled", "disabled"):
        raise _Refused(
            "not_visible", "this credential cannot see the repository's security settings."
        )
    enabled = state == "enabled"
    return observed(
        f"Secret scanning is {'switched on' if enabled else 'switched off'} for {scope}.",
        {"secret_scanning_enabled": enabled},
        raw,
    )


GitHubCheck = Callable[[GitHubClient, str], Awaitable[CheckOutcome]]


def _safely(check: GitHubCheck) -> GitHubCheck:
    """A refusal is a normal result of a check, stored like any other. Not an error."""

    async def run(github: GitHubClient, scope: str) -> CheckOutcome:
        try:
            return await check(github, scope)
        except _Refused as refused:
            return unavailable(refused.reason, refused.detail)

    return run


GITHUB_CHECKS: dict[str, GitHubCheck] = {
    "github.org_two_factor": _safely(org_two_factor),
    "github.branch_protection": _safely(branch_protection),
    "github.secret_scanning": _safely(secret_scanning),
}
