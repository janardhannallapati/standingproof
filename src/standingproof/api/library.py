"""Load the library and search it.

Routes translate between HTTP and the service: they read the request, call one function,
and map its result or its exception to a status code.
"""

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from standingproof.api.deps import EmbedderDep, SessionDep, SettingsDep, TenantDep
from standingproof.library import service
from standingproof.library.embedding import EmbeddingError
from standingproof.library.search import search_library, utc_today
from standingproof.models import LibraryAnswer

router = APIRouter(prefix="/library", tags=["library"])

_UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT
_NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: _NonBlank
    answer: _NonBlank
    # Who to ask when this answer needs reviewing.
    owner: Annotated[_NonBlank, Field(max_length=200)]
    # The last day this answer may be used without a review.
    expires_on: dt.date
    control_id: Annotated[_NonBlank, Field(max_length=64)] | None = None


class AnswerOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str
    owner: str
    expires_on: dt.date
    control_id: str | None
    expired: bool
    created_at: dt.datetime


class AnswerPage(BaseModel):
    items: list[AnswerOut]
    # Pass as `after` to get the next page. Null on the last page.
    next_after: uuid.UUID | None


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    chunk_count: int


class SearchResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["answer", "policy"]
    id: uuid.UUID
    text: str
    question: str | None
    heading_path: str | None
    document_title: str | None
    owner: str | None
    expires_on: dt.date | None
    control_id: str | None
    # Which searches found it: "keyword", "meaning", "control_id".
    found_by: list[str]


class SearchOut(BaseModel):
    results: list[SearchResultOut]


def _answer_out(answer: LibraryAnswer) -> AnswerOut:
    return AnswerOut(
        id=answer.id,
        question=answer.question,
        answer=answer.answer,
        owner=answer.owner,
        expires_on=answer.expires_on,
        control_id=answer.control_id,
        expired=answer.expires_on < utc_today(),
        created_at=answer.created_at,
    )


def _embedding_unavailable(error: EmbeddingError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Embedding failed: {error}")


@router.post("/answers", status_code=status.HTTP_201_CREATED)
async def create_answer(
    body: AnswerIn,
    session: SessionDep,
    tenant_id: TenantDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
) -> AnswerOut:
    try:
        answer = await service.create_answer(
            session,
            tenant_id=tenant_id,
            new=service.NewAnswer(**body.model_dump()),
            embedder=embedder,
            settings=settings,
        )
    except service.LibraryLimitError as error:
        raise HTTPException(_UNPROCESSABLE, str(error)) from error
    except EmbeddingError as error:
        raise _embedding_unavailable(error) from error
    return _answer_out(answer)


@router.get("/answers")
async def list_answers(
    session: SessionDep,
    tenant_id: TenantDep,
    after: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AnswerPage:
    # One more than asked for: its presence says there is a next page.
    rows = await service.page_of_answers(session, tenant_id, after=after, limit=limit + 1)
    page = rows[:limit]
    return AnswerPage(
        items=[_answer_out(row) for row in page],
        next_after=page[-1].id if len(rows) > limit else None,
    )


@router.get("/answers/{answer_id}")
async def get_answer(answer_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> AnswerOut:
    answer = await service.get_answer(session, tenant_id, answer_id)
    if answer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Answer not found.")
    return _answer_out(answer)


@router.post(
    "/policies",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "This file was already uploaded."}},
)
async def upload_policy(
    file: UploadFile,
    response: Response,
    session: SessionDep,
    tenant_id: TenantDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
) -> PolicyOut:
    limit = settings.policy_max_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"The file is larger than the {limit} byte limit."
        )
    try:
        result = await service.ingest_policy(
            session,
            tenant_id=tenant_id,
            filename=file.filename,
            content=content,
            embedder=embedder,
            settings=settings,
        )
    except service.UnsupportedPolicyError as error:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(error)) from error
    except service.LibraryLimitError as error:
        raise HTTPException(_UNPROCESSABLE, str(error)) from error
    except EmbeddingError as error:
        raise _embedding_unavailable(error) from error
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PolicyOut.model_validate(result.doc)


@router.get("/search")
async def search(
    q: Annotated[str, Query(min_length=1, max_length=1000)],
    session: SessionDep,
    tenant_id: TenantDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 5,
) -> SearchOut:
    query = q.strip()
    if not query:
        raise HTTPException(_UNPROCESSABLE, "The query is empty.")
    if limit > settings.search_max_results:
        raise HTTPException(_UNPROCESSABLE, f"limit may be at most {settings.search_max_results}.")
    try:
        hits = await search_library(
            session, tenant_id=tenant_id, query=query, embedder=embedder, limit=limit
        )
    except EmbeddingError as error:
        raise _embedding_unavailable(error) from error
    return SearchOut(results=[SearchResultOut.model_validate(hit) for hit in hits])
