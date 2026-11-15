"""Run a check for a tenant and keep what it saw.

The order is fixed: decide (default deny), reuse a fresh observation if there is one, run
with a time limit, store. Every attempt is stored, including the ones that were refused,
and every query filters by tenant.
"""

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.checks.authorize import authorize, valid_scope
from standingproof.checks.aws import AWS_CHECKS, AwsReader
from standingproof.checks.github import GITHUB_CHECKS, GitHubClient
from standingproof.checks.outcome import CheckOutcome, not_configured, unavailable
from standingproof.checks.registry import CHECKS, CheckDef
from standingproof.config import Settings
from standingproof.models import Connection, Evidence

_SYSTEM_SCOPE_KIND = {"github": "org", "aws": "account"}
_MAX_NAME_CHARS = 100
_MAX_SCOPE_CHARS = 300


class InvalidConnectionError(Exception):
    """The system is unknown, or the scope is not an organisation name or an account id."""


@dataclass
class CheckBackends:
    """The clients checks run through. None means that system has no credential configured."""

    github: GitHubClient | None
    aws: AwsReader | None

    async def aclose(self) -> None:
        if self.github is not None:
            await self.github.aclose()


def build_backends(settings: Settings) -> CheckBackends:
    github = None
    if settings.github_token is not None and settings.github_token.get_secret_value():
        github = GitHubClient(
            token=settings.github_token.get_secret_value(), base_url=settings.github_api_url
        )
    # The AWS SDK finds its own credentials, on first use. With none it reports that.
    return CheckBackends(github=github, aws=AwsReader(region=settings.aws_region))


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def source_text(evidence: Evidence) -> str:
    """The sentence an answer may cite: the fact, and when it was seen."""
    return f"{evidence.statement} Observed {evidence.observed_at:%Y-%m-%d %H:%M} UTC."


async def add_connection(
    session: AsyncSession, *, tenant_id: uuid.UUID, system: str, scope: str
) -> tuple[Connection, bool]:
    """Allow this tenant to run checks on a scope. Returns the connection and whether it is new."""
    kind = _SYSTEM_SCOPE_KIND.get(system)
    if kind is None or not valid_scope(kind, scope):
        raise InvalidConnectionError(
            "system must be 'github' with an organisation name, "
            "or 'aws' with a 12-digit account id."
        )
    scope = scope.lower()
    created = (
        await session.execute(
            insert(Connection)
            .values(tenant_id=tenant_id, system=system, scope=scope)
            .on_conflict_do_nothing(constraint="uq_connections_tenant_scope")
            .returning(Connection.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    connection = (
        await session.execute(
            select(Connection).where(
                Connection.tenant_id == tenant_id,
                Connection.system == system,
                Connection.scope == scope,
            )
        )
    ).scalar_one()
    return connection, created is not None


async def list_connections(session: AsyncSession, tenant_id: uuid.UUID) -> list[Connection]:
    rows = await session.execute(
        select(Connection)
        .where(Connection.tenant_id == tenant_id)
        .order_by(Connection.system, Connection.scope)
    )
    return list(rows.scalars())


async def _fresh(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    check: CheckDef,
    scope: str,
    not_before: dt.datetime,
) -> Evidence | None:
    # Only a real observation is reused. A failure is always tried again.
    rows = await session.execute(
        select(Evidence)
        .where(
            Evidence.tenant_id == tenant_id,
            Evidence.check_name == check.name,
            Evidence.scope == scope,
            Evidence.status == "observed",
            Evidence.observed_at > not_before,
        )
        .order_by(Evidence.observed_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _execute(check: CheckDef, scope: str, backends: CheckBackends) -> CheckOutcome:
    if check.system == "github":
        if backends.github is None:
            return not_configured("no GitHub credential is configured.")
        return await GITHUB_CHECKS[check.name](backends.github, scope)
    if backends.aws is None:
        return not_configured("no AWS credential is configured.")
    return await AWS_CHECKS[check.name](backends.aws, scope)


async def run_check(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    scope: str,
    backends: CheckBackends,
    settings: Settings,
) -> Evidence:
    """Decide, then look. Returns the stored evidence, whatever the outcome was."""
    check = CHECKS.get(name)
    connections = [(c.system, c.scope) for c in await list_connections(session, tenant_id)]
    decision = authorize(check, scope, connections)
    now = utc_now()

    if check is None or not decision.allowed:
        outcome = CheckOutcome("denied", f"Not run: {decision.reason}.", reason=decision.reason)
    else:
        if check.system == "github":
            scope = scope.lower()  # GitHub names are case-insensitive; one spelling per scope
        max_age = dt.timedelta(seconds=settings.evidence_max_age_seconds)
        fresh = await _fresh(session, tenant_id, check, scope, now - max_age)
        if fresh is not None:
            return fresh
        try:
            async with asyncio.timeout(settings.check_timeout_seconds):
                outcome = await _execute(check, scope, backends)
        except TimeoutError:
            outcome = unavailable(
                "timed_out", f"no answer within {settings.check_timeout_seconds:g} seconds."
            )
        now = utc_now()

    evidence = Evidence(
        tenant_id=tenant_id,
        # What was asked for is recorded even when it was refused, cut to the column's size.
        check_name=name[:_MAX_NAME_CHARS],
        scope=scope[:_MAX_SCOPE_CHARS],
        status=outcome.status,
        facts=outcome.facts,
        raw_result=outcome.raw,
        statement=outcome.statement,
        reason=outcome.reason,
        observed_at=now,
    )
    session.add(evidence)
    await session.commit()
    return evidence


async def get_evidence(
    session: AsyncSession, tenant_id: uuid.UUID, evidence_id: uuid.UUID
) -> Evidence | None:
    """Return the evidence, or None if it does not exist *for this tenant*."""
    return (
        await session.execute(
            select(Evidence).where(Evidence.tenant_id == tenant_id, Evidence.id == evidence_id)
        )
    ).scalar_one_or_none()


async def page_of_evidence(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    check_name: str | None,
    scope: str | None,
    before: dt.datetime | None,
    limit: int,
) -> list[Evidence]:
    """Up to `limit` rows observed before `before`, newest first."""
    statement = select(Evidence).where(Evidence.tenant_id == tenant_id)
    if check_name is not None:
        statement = statement.where(Evidence.check_name == check_name)
    if scope is not None:
        statement = statement.where(Evidence.scope == scope.lower())
    if before is not None:
        statement = statement.where(Evidence.observed_at < before)
    rows = await session.execute(
        statement.order_by(Evidence.observed_at.desc(), Evidence.id).limit(limit)
    )
    return list(rows.scalars())
