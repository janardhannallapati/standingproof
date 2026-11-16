"""The filled file: what it will contain, making it once, and downloading it."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from standingproof.api.deps import SessionDep, SettingsDep, TenantDep
from standingproof.writeback import sent, service
from standingproof.writeback.writer import WritebackError

router = APIRouter(tags=["files"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_UNPROCESSABLE = 422


class FilledFileOut(BaseModel):
    id: uuid.UUID
    sha256: str
    size_bytes: int
    created_at: datetime
    created_by: str
    # False when a decision changed after this file was made.
    current: bool


class WrittenCellOut(BaseModel):
    position: int
    sheet: str
    cell: str
    question: str
    wording: str
    # approved | edited
    how: str
    # answer | comment (the same wording, in the comments column) | choice (a dropdown's
    # value, taken from the first word of the approved wording)
    kind: str


class EmptyCellOut(BaseModel):
    position: int
    sheet: str
    cell: str | None
    question: str
    # undecided | rejected | expired | unanswered | no_answer | no_answer_cell |
    # sheet_missing | cell_not_empty | dropdown | too_long | bad_character
    reason: str
    # For `dropdown`: the values the cell accepts. For `too_long`: the length.
    detail: str
    # The reviewer's note on a rejected question.
    comment: str | None


class SentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Who the file was sent to. Free text; shown back as typed.
    customer: str = Field(min_length=1, max_length=sent.MAX_CUSTOMER_CHARS)

    @field_validator("customer")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("say who the file was sent to")
        return value.strip()


class SentOut(BaseModel):
    id: uuid.UUID
    customer: str
    sent_by: str
    sent_at: datetime
    # How many questions' wording went out in the file.
    answers: int
    # False when a decision changed after the file that was sent was made.
    current: bool
    # How many of those answers wait again because a live setting changed.
    reopened: int = 0


class FilePreviewOut(BaseModel):
    filename: str
    ready: bool
    undecided: int
    written: list[WrittenCellOut]
    left_empty: list[EmptyCellOut]
    protected_sheets: list[str]
    latest: FilledFileOut | None
    # Each time a file of this run was recorded as sent, oldest first.
    sent: list[SentOut]


def _file_out(info: service.FileInfo) -> FilledFileOut:
    return FilledFileOut(
        id=info.id,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        created_at=info.created_at,
        created_by=info.created_by,
        current=info.current,
    )


def _sent_out(info: sent.SentInfo) -> SentOut:
    return SentOut(
        id=info.id,
        customer=info.customer,
        sent_by=info.sent_by,
        sent_at=info.sent_at,
        answers=info.answers,
        current=info.current,
        reopened=info.reopened,
    )


@router.get("/runs/{run_id}/file/preview", responses={404: {"description": "No such run."}})
async def preview_file(
    run_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep
) -> FilePreviewOut:
    """Which cells a file made now would fill, and which it would leave empty and why.

    Stores nothing. Text here comes from the questionnaire and from reviewers: show it as
    text.
    """
    found = await service.preview(session, tenant_id, run_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    rows = {row.position: row for row in found.rows}
    return FilePreviewOut(
        filename=found.filename,
        ready=found.undecided == 0,
        undecided=found.undecided,
        written=[
            WrittenCellOut(
                position=write.position,
                sheet=write.sheet,
                cell=write.cell,
                question=rows[write.position].question,
                wording=write.wording,
                how=rows[write.position].review or "",
                kind=write.kind,
            )
            for write in found.plan.writes
        ],
        left_empty=[
            EmptyCellOut(
                position=skip.position,
                sheet=skip.sheet,
                cell=skip.cell,
                question=rows[skip.position].question,
                reason=skip.reason,
                detail=skip.detail,
                comment=rows[skip.position].comment if skip.reason == "rejected" else None,
            )
            for skip in found.plan.skips
        ],
        protected_sheets=found.protected_sheets,
        latest=_file_out(found.latest) if found.latest else None,
        sent=[_sent_out(each) for each in found.sent],
    )


@router.post(
    "/runs/{run_id}/file",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "This file was already made for these decisions."},
        404: {"description": "No such run."},
        409: {"description": "A question is undecided, or its wording changed after its decision."},
        422: {"description": "The workbook could not be written to safely. Nothing was made."},
    },
)
async def make_file(
    run_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
) -> FilledFileOut:
    """Write the approved answers into the uploaded workbook, once.

    Safe to repeat: the same decisions always give the same file, and a second request
    (after a timeout, a double click, a crash) returns the first result with 200.
    """
    try:
        made = await service.make_file(session, tenant_id, run_id, created_by=settings.dev_reviewer)
    except (service.NotAllDecidedError, service.WordingMismatchError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except WritebackError as error:
        raise HTTPException(_UNPROCESSABLE, f"The file was not made: {error}") from error
    if made is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    info, created = made
    if not created:
        response.status_code = status.HTTP_200_OK
    return _file_out(info)


@router.get(
    "/runs/{run_id}/file/{file_id}",
    response_class=Response,
    responses={200: {"content": {XLSX: {}}}, 404: {"description": "No such file."}},
)
async def download_file(
    run_id: uuid.UUID, file_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep
) -> Response:
    """The filled workbook's bytes."""
    found = await service.read_file(session, tenant_id, run_id, file_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    name, content = found
    return Response(
        content=content,
        media_type=XLSX,
        headers={
            # The name was reduced to letters, digits and `._- ` before it got here.
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/runs/{run_id}/file/{file_id}/sent",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "This file was already recorded as sent to this customer."},
        404: {"description": "No such file."},
        409: {"description": "A decision changed after this file was made."},
    },
)
async def record_sent(
    run_id: uuid.UUID,
    file_id: uuid.UUID,
    body: SentIn,
    response: Response,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
) -> SentOut:
    """Record that a person sent this file to a customer. Append-only, and safe to repeat.

    It records what a reviewer said. It does not send anything and does not say the file
    was delivered.
    """
    try:
        recorded = await sent.record_sent(
            session,
            tenant_id,
            run_id,
            file_id,
            customer=body.customer,
            sent_by=settings.dev_reviewer,
        )
    except sent.NotCurrentFileError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if recorded is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    info, created = recorded
    if not created:
        response.status_code = status.HTTP_200_OK
    return _sent_out(info)
