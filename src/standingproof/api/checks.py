"""Connect a scope, run a read-only check on it, and read back what was observed."""

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from standingproof.api.deps import BackendsDep, SessionDep, SettingsDep, TenantDep
from standingproof.checks import service
from standingproof.checks.registry import CHECKS

router = APIRouter(tags=["checks"])


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    system: Literal["github", "aws"]
    scope_kind: Literal["org", "repo", "account"]
    description: str


class CheckList(BaseModel):
    checks: list[CheckOut]


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str = Field(max_length=16)
    # A GitHub organisation name, or a 12-digit AWS account id.
    scope: str = Field(max_length=100)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    system: str
    scope: str


class ConnectionList(BaseModel):
    connections: list[ConnectionOut]


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(max_length=300)


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    check_name: str
    scope: str
    status: Literal["observed", "unavailable", "not_configured", "denied"]
    # One sentence stating what was seen, or that nothing was.
    statement: str
    facts: dict[str, Any]
    raw_result: dict[str, Any]
    reason: str | None
    observed_at: dt.datetime


class EvidencePage(BaseModel):
    items: list[EvidenceOut]
    # Pass as `before` to get older rows. Null on the last page.
    next_before: dt.datetime | None


@router.get("/checks")
async def list_checks() -> CheckList:
    return CheckList(checks=[CheckOut.model_validate(check) for check in CHECKS.values()])


@router.post(
    "/connections",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "This scope was already connected."},
        403: {"description": "The operator has not declared this scope."},
        409: {"description": "Another tenant holds this scope."},
    },
)
async def connect(
    body: ConnectionIn,
    response: Response,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
) -> ConnectionOut:
    try:
        connection, created = await service.add_connection(
            session, tenant_id=tenant_id, system=body.system, scope=body.scope, settings=settings
        )
    except service.InvalidConnectionError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except service.ScopeNotDeclaredError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except service.ScopeTakenError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if not created:
        response.status_code = status.HTTP_200_OK
    return ConnectionOut.model_validate(connection)


@router.get("/connections")
async def list_connections(session: SessionDep, tenant_id: TenantDep) -> ConnectionList:
    rows = await service.list_connections(session, tenant_id)
    return ConnectionList(connections=[ConnectionOut.model_validate(row) for row in rows])


@router.post("/checks/{name}/run")
async def run_check(
    name: str,
    body: RunIn,
    session: SessionDep,
    tenant_id: TenantDep,
    backends: BackendsDep,
    settings: SettingsDep,
) -> EvidenceOut:
    evidence = await service.run_check(
        session,
        tenant_id=tenant_id,
        name=name,
        scope=body.scope,
        backends=backends,
        settings=settings,
        trigger="manual",
    )
    if evidence.status == "denied":
        # The attempt is already stored. The caller is told why, and nothing was called.
        if evidence.reason == "unknown_check":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no check with that name.")
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Not allowed: {evidence.reason}.")
    return EvidenceOut.model_validate(evidence)


@router.get("/evidence")
async def list_evidence(
    session: SessionDep,
    tenant_id: TenantDep,
    check: Annotated[str | None, Query(max_length=100)] = None,
    scope: Annotated[str | None, Query(max_length=300)] = None,
    before: dt.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EvidencePage:
    rows = await service.page_of_evidence(
        session, tenant_id, check_name=check, scope=scope, before=before, limit=limit + 1
    )
    page = rows[:limit]
    return EvidencePage(
        items=[EvidenceOut.model_validate(row) for row in page],
        next_before=page[-1].observed_at if len(rows) > limit else None,
    )


@router.get("/evidence/{evidence_id}")
async def get_evidence(
    evidence_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep
) -> EvidenceOut:
    evidence = await service.get_evidence(session, tenant_id, evidence_id)
    if evidence is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found.")
    return EvidenceOut.model_validate(evidence)
