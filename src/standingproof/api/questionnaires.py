"""Upload a questionnaire and read back its questions.

Routes translate between HTTP and the service: they read the request, call one service
function, and map its result or its exception to a status code. No parsing happens here.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict

from standingproof.api.deps import SessionDep, SettingsDep, TenantDep
from standingproof.models import STATUS_NEEDS_MAPPING, Questionnaire
from standingproof.questionnaires import service
from standingproof.questionnaires.intake import FileLimitError, UnsupportedFileError
from standingproof.questionnaires.parser import ManualMapping, MappingError, SheetPreview

router = APIRouter(prefix="/questionnaires", tags=["questionnaires"])

_UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT


class SheetPreviewOut(BaseModel):
    name: str
    rows: list[list[str]]


class QuestionnaireOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: Literal["parsed", "needs_mapping"]
    question_count: int
    # Present only while a person still has to choose the columns.
    sheets: list[SheetPreviewOut] | None


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    sheet: str
    cell: str
    text: str
    answer_cell: str | None
    external_id: str | None
    truncated: bool


class QuestionPage(BaseModel):
    items: list[QuestionOut]
    # Pass as `after` to get the next page. Null on the last page.
    next_after: int | None


def _out(questionnaire: Questionnaire, previews: list[SheetPreview] | None) -> QuestionnaireOut:
    return QuestionnaireOut(
        id=questionnaire.id,
        filename=questionnaire.filename,
        status=questionnaire.status,  # type: ignore[arg-type]
        question_count=questionnaire.question_count,
        sheets=(
            [SheetPreviewOut(name=p.name, rows=p.rows) for p in previews]
            if previews is not None
            else None
        ),
    )


async def _owned(
    session: SessionDep, tenant_id: TenantDep, questionnaire_id: uuid.UUID
) -> Questionnaire:
    questionnaire = await service.get(session, tenant_id, questionnaire_id)
    if questionnaire is None:
        # The same answer for "does not exist" and "belongs to someone else", so the
        # response never confirms that another tenant's id is real.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Questionnaire not found.")
    return questionnaire


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": QuestionnaireOut, "description": "This exact file was uploaded before."},
        413: {"description": "The file is larger than the upload limit."},
        415: {"description": "The file is not a plain .xlsx workbook."},
        422: {"description": "The workbook is unreadable or over a parsing limit."},
    },
)
async def upload_questionnaire(
    file: UploadFile,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
    response: Response,
) -> QuestionnaireOut:
    """Upload one .xlsx questionnaire as multipart form field `file`."""
    limit = settings.upload_max_bytes
    content = await file.read(limit + 1)  # one byte past the limit is enough to know
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"The file is larger than the limit of {limit} bytes.",
        )

    try:
        result = await service.ingest(
            session,
            tenant_id=tenant_id,
            filename=file.filename,
            content=content,
            settings=settings,
        )
    except UnsupportedFileError as error:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(error)) from error
    except (FileLimitError, service.UnreadableWorkbookError) as error:
        raise HTTPException(_UNPROCESSABLE, str(error)) from error

    questionnaire = result.questionnaire
    if not result.created:
        response.status_code = status.HTTP_200_OK
    needs_mapping = questionnaire.status == STATUS_NEEDS_MAPPING
    previews = result.previews
    if needs_mapping and not result.created:
        previews = await service.previews(session, questionnaire, settings)
    return _out(questionnaire, previews if needs_mapping else None)


@router.get("/{questionnaire_id}")
async def get_questionnaire(
    questionnaire_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep, settings: SettingsDep
) -> QuestionnaireOut:
    questionnaire = await _owned(session, tenant_id, questionnaire_id)
    if questionnaire.status != STATUS_NEEDS_MAPPING:
        return _out(questionnaire, None)
    return _out(questionnaire, await service.previews(session, questionnaire, settings))


@router.get("/{questionnaire_id}/questions")
async def list_questions(
    questionnaire_id: uuid.UUID,
    session: SessionDep,
    tenant_id: TenantDep,
    after: Annotated[int, Query(ge=0, description="`next_after` from the previous page.")] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> QuestionPage:
    questionnaire = await _owned(session, tenant_id, questionnaire_id)
    rows, next_after = await service.page_of_questions(
        session, questionnaire, after=after, limit=limit
    )
    return QuestionPage(
        items=[QuestionOut.model_validate(row) for row in rows], next_after=next_after
    )


@router.put(
    "/{questionnaire_id}/mapping",
    responses={
        409: {"description": "The questionnaire has already been parsed."},
        422: {"description": "The mapping does not fit the file."},
    },
)
async def set_mapping(
    questionnaire_id: uuid.UUID,
    mapping: ManualMapping,
    session: SessionDep,
    tenant_id: TenantDep,
    settings: SettingsDep,
) -> QuestionnaireOut:
    """Choose the question column by hand when it could not be detected."""
    questionnaire = await _owned(session, tenant_id, questionnaire_id)
    try:
        questionnaire = await service.apply_mapping(session, questionnaire, mapping, settings)
    except service.AlreadyParsedError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except (MappingError, FileLimitError, service.UnreadableWorkbookError) as error:
        raise HTTPException(_UNPROCESSABLE, str(error)) from error
    return _out(questionnaire, None)
