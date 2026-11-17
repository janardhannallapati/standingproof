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

from standingproof.audit import Entry, record
from standingproof.checks.authorize import authorize, valid_scope
from standingproof.checks.aws import AWS_CHECKS, AwsReader
from standingproof.checks.github import GITHUB_CHECKS, GitHubClient
from standingproof.checks.outcome import CheckOutcome, not_configured, unavailable
from standingproof.checks.registry import CHECKS, CheckDef
from standingproof.config import Settings
from standingproof.limits import switch
from standingproof.models import Connection, DriftQueue, Evidence

_SYSTEM_SCOPE_KIND = {"github": "org", "aws": "account"}
_MAX_NAME_CHARS = 100
_MAX_SCOPE_CHARS = 300


class InvalidConnectionError(Exception):
    """The system is unknown, or the scope is not an organisation name or an account id."""


class ScopeNotDeclaredError(Exception):
    """The operator has not declared this scope for the deployment's credentials."""


class ScopeTakenError(Exception):
    """Another tenant has already connected this scope."""


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
    session: AsyncSession, *, tenant_id: uuid.UUID, system: str, scope: str, settings: Settings
) -> tuple[Connection, bool]:
    """Allow this tenant to run checks on a scope. Returns the connection and whether it is new.

    The credentials that checks run with belong to the deployment, not to the tenant. So a
    tenant cannot grant itself a scope: the operator must have declared it, and it can be
    held by one tenant only. Per-tenant credentials will replace both rules.
    """
    kind = _SYSTEM_SCOPE_KIND.get(system)
    if kind is None or not valid_scope(kind, scope):
        raise InvalidConnectionError(
            "system must be 'github' with an organisation name, "
            "or 'aws' with a 12-digit account id."
        )
    scope = scope.lower()
    if scope not in settings.declared_scopes(system):
        raise ScopeNotDeclaredError(
            "This deployment's credentials are not declared for that scope. "
            "The operator lists permitted scopes in SP_GITHUB_ORGS and SP_AWS_ACCOUNTS."
        )
    created = (
        await session.execute(
            insert(Connection)
            .values(tenant_id=tenant_id, system=system, scope=scope)
            .on_conflict_do_nothing(constraint="uq_connections_scope")
            .returning(Connection.id)
        )
    ).scalar_one_or_none()
    if created is not None:
        await record(
            session,
            tenant_id,
            Entry(action="connection.add", subject=f"{system}:{scope}", details={}),
        )
    await session.commit()
    # The unique constraint spans every tenant; this session sees only its own rows. So a
    # scope that could not be inserted and cannot be seen is held by another tenant. The
    # caller learns that it is taken and nothing else.
    holder = (
        await session.execute(
            select(Connection).where(Connection.system == system, Connection.scope == scope)
        )
    ).scalar_one_or_none()
    if holder is None or holder.tenant_id != tenant_id:
        raise ScopeTakenError("That scope is already connected.")
    return holder, created is not None


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
        .order_by(Evidence.seq.desc())
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
    max_age: dt.timedelta | None = None,
    trigger: str = "run",
) -> Evidence:
    """Decide, then look. Returns the stored evidence, whatever the outcome was.

    `max_age` is how old an observation may be and still be returned with no new look;
    the default is the deployment's setting, and zero always looks. `trigger` records why
    the look was made: run (an agent drafting), schedule, webhook or manual.
    """
    check = CHECKS.get(name)
    # A connection counts only while the operator still declares its scope.
    connections = [
        (c.system, c.scope)
        for c in await list_connections(session, tenant_id)
        if c.scope in settings.declared_scopes(c.system)
    ]
    decision = authorize(check, scope, connections)
    now = utc_now()

    if (await switch.read(session, tenant_id, settings)).stopped:
        # The stop switch is on: nothing of ours reaches an outside system. Recorded like
        # any other refusal, so "why is there no observation?" has an answer.
        outcome = CheckOutcome("denied", "Not run: stopped.", reason="stopped")
    elif check is None or not decision.allowed:
        outcome = CheckOutcome("denied", f"Not run: {decision.reason}.", reason=decision.reason)
    else:
        if check.system == "github":
            scope = scope.lower()  # GitHub names are case-insensitive; one spelling per scope
        if max_age is None:
            max_age = dt.timedelta(seconds=settings.evidence_max_age_seconds)
        # Zero means "look now", and is not asked of the clock: an observation stamped
        # after a clock that has since stepped back is "newer than now" and would be
        # handed back as the new look.
        fresh = await _fresh(session, tenant_id, check, scope, now - max_age) if max_age else None
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
    if outcome.status == "observed":
        # In the same transaction as the observation: whoever compares observations with
        # the answers that rest on them (standingproof.drift) can never miss this one.
        await session.flush()
        session.add(DriftQueue(evidence_id=evidence.id, tenant_id=tenant_id, trigger=trigger))
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
    before_seq: int | None,
    limit: int,
) -> list[Evidence]:
    """Up to `limit` rows stored before the one numbered `before_seq`, newest first."""
    statement = select(Evidence).where(Evidence.tenant_id == tenant_id)
    if check_name is not None:
        statement = statement.where(Evidence.check_name == check_name)
    if scope is not None:
        statement = statement.where(Evidence.scope == scope.lower())
    if before_seq is not None:
        statement = statement.where(Evidence.seq < before_seq)
    rows = await session.execute(statement.order_by(Evidence.seq.desc()).limit(limit))
    return list(rows.scalars())
