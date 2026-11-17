"""Build one case's world: a real database and the real tools, in front of a GitHub and an
AWS that exist only here.

The library and policies go into Postgres through the product's own services, and the
drafter gets the product's own tools. The two systems a live check reads are replaced at the
last step before the network: GitHub by a transport that answers from the case, AWS by
clients that do the same. Nothing in this module can build a real client.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import httpx2
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.cases import DrafterCase, DrafterSet, Live
from evals.fingerprint import DrafterVariant
from evals.fingerprint import settings as plain_settings
from standingproof.agent.tools import ToolRegistry
from standingproof.checks.aws import AwsReader
from standingproof.checks.github import GitHubClient
from standingproof.checks.service import CheckBackends, add_connection
from standingproof.checks.tools import check_tool
from standingproof.library.embedding import Embedder
from standingproof.library.service import NewAnswer, create_answer, ingest_policy
from standingproof.library.tools import build_library_registry
from standingproof.models import Tenant

# Emptying a throwaway database between cases and between tests. The audit log refuses to
# be emptied, by design, so this says so on purpose: as the owner, it switches that refusal
# off for the statement. Never run against a database that holds a real record.
EMPTY_THE_DATABASE = (
    "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate",
    "ALTER TABLE audit_heads DISABLE TRIGGER audit_heads_no_truncate",
    "TRUNCATE tenants CASCADE",
    "ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate",
    "ALTER TABLE audit_heads ENABLE TRIGGER audit_heads_no_truncate",
)


_OWNER = "security@harborlight.example"
_REGION = "us-east-1"
_AWS_ERRORS = {"forbidden": "AccessDenied", "rate_limited": "Throttling"}
_HTTP_ERRORS = {"forbidden": 403, "rate_limited": 429}


def _github(lives: list[Live]) -> httpx2.MockTransport:
    """A GitHub that holds exactly the settings the case names."""
    by_check = {(live.check, live.scope.lower()): live for live in lives if live.system == "github"}

    def failed(live: Live) -> httpx2.Response:
        if live.fails == "unreachable":
            raise httpx2.ConnectError("no route to the eval's GitHub")
        return httpx2.Response(_HTTP_ERRORS[live.fails or "forbidden"], json={"message": "No"})

    def handler(request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.strip("/").split("/")
        missing = httpx2.Response(404, json={"message": "Not Found"})
        if parts[0] == "orgs" and len(parts) == 2:
            org = by_check.get(("github.org_two_factor", parts[1].lower()))
            if org is None:
                return missing
            if org.reads is None:
                return failed(org)
            return httpx2.Response(
                200, json={"two_factor_requirement_enabled": org.reads["required"]}
            )
        if parts[0] != "repos" or len(parts) < 3:
            return missing
        scope = f"{parts[1]}/{parts[2]}".lower()
        branch = by_check.get(("github.branch_protection", scope))
        scanning = by_check.get(("github.secret_scanning", scope))
        if branch is None and scanning is None:
            return missing
        if len(parts) == 3:
            if scanning is not None and scanning.reads is None:
                return failed(scanning)
            repository: dict[str, Any] = {"default_branch": "main"}
            if scanning is not None and scanning.reads is not None:
                state = "enabled" if scanning.reads["enabled"] else "disabled"
                repository["security_and_analysis"] = {"secret_scanning": {"status": state}}
            return httpx2.Response(200, json=repository)
        if parts[3:] == ["branches", "main", "protection"] and branch is not None:
            if branch.reads is None:
                return failed(branch)
            if not branch.reads["protected"]:
                return missing
            return httpx2.Response(
                200,
                json={
                    "required_pull_request_reviews": {
                        "required_approving_review_count": branch.reads["reviews"]
                    },
                    "enforce_admins": {"enabled": branch.reads["admins"]},
                },
            )
        return missing

    return httpx2.MockTransport(handler)


class _Aws:
    """The five AWS calls the checks make, answered from the case."""

    def __init__(self, lives: list[Live]) -> None:
        self._by_check = {live.check: live for live in lives if live.system == "aws"}
        accounts = [live.scope for live in lives if live.system == "aws"]
        self._account = accounts[0] if accounts else "000000000000"

    def client(self, _service: str) -> "_Aws":
        return self

    def _reads(self, check: str, operation: str) -> dict[str, Any]:
        live = self._by_check.get(check)
        if live is None or live.fails in _AWS_ERRORS:
            code = _AWS_ERRORS[live.fails] if live and live.fails else "AccessDenied"
            raise ClientError({"Error": {"Code": code}}, operation)
        if live.reads is None:
            raise EndpointConnectionError(endpoint_url="https://aws.invalid")
        return dict(live.reads)

    def get_caller_identity(self) -> dict[str, Any]:
        return {"Account": self._account}

    def get_account_summary(self) -> dict[str, Any]:
        reads = self._reads("aws.root_mfa", "GetAccountSummary")
        return {"SummaryMap": {"AccountMFAEnabled": 1 if reads["enabled"] else 0}}

    def get_account_password_policy(self) -> dict[str, Any]:
        reads = self._reads("aws.password_policy", "GetAccountPasswordPolicy")
        if not reads["set"]:
            raise ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetAccountPasswordPolicy")
        policy = {"MinimumPasswordLength": reads["minimum_length"], "RequireSymbols": False}
        if reads["max_age_days"] is not None:
            policy["MaxPasswordAge"] = reads["max_age_days"]
        return {"PasswordPolicy": policy}

    def get_public_access_block(self, **_: Any) -> dict[str, Any]:
        blocked = self._reads("aws.s3_public_access_block", "GetPublicAccessBlock")["blocked"]
        names = (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
        return {"PublicAccessBlockConfiguration": dict.fromkeys(names, blocked)}

    def get_ebs_encryption_by_default(self) -> dict[str, Any]:
        reads = self._reads("aws.ebs_default_encryption", "GetEbsEncryptionByDefault")
        return {"EbsEncryptionByDefault": reads["encrypted"]}


def fake_backends(lives: list[Live]) -> CheckBackends:
    return CheckBackends(
        github=GitHubClient(
            token="eval-not-a-token",  # noqa: S106 - sent only to the transport above
            base_url="https://github.invalid",
            client=httpx2.AsyncClient(transport=_github(lives)),
        ),
        aws=AwsReader(region=_REGION, client_factory=_Aws(lives).client),
    )


@dataclass
class World:
    tenant_id: uuid.UUID
    registry: ToolRegistry
    backends: CheckBackends

    async def aclose(self) -> None:
        await self.backends.aclose()


async def build_world(
    sessions: async_sessionmaker[AsyncSession],
    cases: DrafterSet,
    case: DrafterCase,
    *,
    embedder: Embedder,
    variant: DrafterVariant = "full",
) -> World:
    """Empty the database, then put this case's company in it. One world at a time."""
    settings = plain_settings(
        github_orgs=",".join(case.connected.get("github", [])),
        aws_accounts=",".join(case.connected.get("aws", [])),
        aws_region=_REGION,
        # Every look in an eval is a new look.
        evidence_max_age_seconds=0,
    )
    library = cases.library(case.library)
    valid_until = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=365)
    async with sessions() as session:
        for statement in EMPTY_THE_DATABASE:
            await session.execute(text(statement))
        tenant = Tenant(name=f"eval-{case.name}")
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id
        for answer in library.answers:
            new = NewAnswer(answer.question, answer.answer, _OWNER, valid_until, answer.control_id)
            await create_answer(
                session, tenant_id=tenant_id, new=new, embedder=embedder, settings=settings
            )
        for policy in library.policies:
            await ingest_policy(
                session,
                tenant_id=tenant_id,
                filename=policy.name,
                content=policy.text.encode(),
                embedder=embedder,
                settings=settings,
            )
        for system, scopes in case.connected.items():
            for scope in scopes:
                await add_connection(
                    session, tenant_id=tenant_id, system=system, scope=scope, settings=settings
                )
    backends = fake_backends(case.live)
    registry = build_library_registry(sessions, tenant_id, embedder)
    if variant == "full":
        registry = registry.with_tools(check_tool(sessions, tenant_id, backends, settings))
    return World(tenant_id=tenant_id, registry=registry, backends=backends)
