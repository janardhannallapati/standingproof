"""Store an uploaded questionnaire and its questions.

The only module that joins the pure parsing code to the database. Every query filters by
tenant, and every change is committed here, before the caller builds a response.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.config import Settings
from standingproof.models import (
    STATUS_NEEDS_MAPPING,
    STATUS_PARSED,
    Question,
    Questionnaire,
    QuestionnaireFile,
)
from standingproof.questionnaires.intake import FileLimitError, check_xlsx
from standingproof.questionnaires.parser import (
    ManualMapping,
    MappingError,
    ParsedQuestion,
    ParsedWorkbook,
    SheetPreview,
    clean_text,
    parse_workbook,
)

log = logging.getLogger(__name__)

_DEFAULT_FILENAME = "questionnaire.xlsx"
_MAX_FILENAME_CHARS = 255


class UnreadableWorkbookError(Exception):
    """The file passed the intake checks but the spreadsheet parser could not read it."""


class AlreadyParsedError(Exception):
    """A mapping was submitted for a questionnaire that does not need one."""


@dataclass(frozen=True)
class Ingested:
    questionnaire: Questionnaire
    created: bool


def _parse(content: bytes, settings: Settings, mapping: ManualMapping | None) -> ParsedWorkbook:
    try:
        return parse_workbook(
            content,
            max_questions=settings.upload_max_questions,
            max_chars=settings.question_max_chars,
            max_rows=settings.upload_max_rows_per_sheet,
            max_sheets=settings.upload_max_sheets,
            mapping=mapping,
        )
    except (FileLimitError, MappingError):
        raise
    except Exception as error:
        # A third-party parser reading a hostile or damaged file can fail in many ways.
        # All of them mean the same thing to the uploader, and none may crash the request.
        log.warning("workbook could not be parsed: %s", type(error).__name__)
        raise UnreadableWorkbookError("the workbook could not be read") from error


async def _parse_in_thread(
    content: bytes, settings: Settings, mapping: ManualMapping | None = None
) -> ParsedWorkbook:
    # Parsing is CPU work with no await in it. On the event loop it would stall every other
    # request for its whole duration, so it runs in a worker thread.
    return await asyncio.to_thread(_parse, content, settings, mapping)


def _safe_filename(name: str | None) -> str:
    # Stored and shown as text only. It is never used to build a path.
    cleaned = clean_text(name or "").replace("\n", " ").replace("\t", " ")
    return cleaned[:_MAX_FILENAME_CHARS] or _DEFAULT_FILENAME


def _rows(
    questions: list[ParsedQuestion], *, tenant_id: uuid.UUID, questionnaire_id: uuid.UUID
) -> list[Question]:
    return [
        Question(
            tenant_id=tenant_id,
            questionnaire_id=questionnaire_id,
            position=position,
            sheet=q.sheet,
            cell=q.cell,
            text=q.text,
            answer_cell=q.answer_cell,
            comment_cell=q.comment_cell,
            external_id=q.external_id,
            truncated=q.truncated,
        )
        for position, q in enumerate(questions, start=1)
    ]


def _stored_previews(previews: list[SheetPreview]) -> list[dict[str, Any]]:
    return [{"name": preview.name, "rows": preview.rows} for preview in previews]


async def _by_hash(
    session: AsyncSession, tenant_id: uuid.UUID, sha256: str
) -> Questionnaire | None:
    return (
        await session.execute(
            select(Questionnaire).where(
                Questionnaire.tenant_id == tenant_id, Questionnaire.sha256 == sha256
            )
        )
    ).scalar_one_or_none()


async def get(
    session: AsyncSession, tenant_id: uuid.UUID, questionnaire_id: uuid.UUID
) -> Questionnaire | None:
    """Return the questionnaire, or None if it does not exist *for this tenant*."""
    return (
        await session.execute(
            select(Questionnaire).where(
                Questionnaire.tenant_id == tenant_id, Questionnaire.id == questionnaire_id
            )
        )
    ).scalar_one_or_none()


async def ingest(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str | None,
    content: bytes,
    settings: Settings,
) -> Ingested:
    """Check, store and parse one upload. Uploading the same bytes again changes nothing."""
    sha256 = hashlib.sha256(content).hexdigest()
    existing = await _by_hash(session, tenant_id, sha256)
    if existing is not None:
        return Ingested(existing, created=False)

    check_xlsx(
        content,
        max_uncompressed_bytes=settings.upload_max_uncompressed_bytes,
        max_sheets=settings.upload_max_sheets,
    )
    parsed = await _parse_in_thread(content, settings)

    questionnaire = Questionnaire(
        tenant_id=tenant_id,
        filename=_safe_filename(filename),
        status=STATUS_PARSED if parsed.questions else STATUS_NEEDS_MAPPING,
        sha256=sha256,
        question_count=len(parsed.questions),
        mapping=parsed.mappings or None,
        previews=None if parsed.questions else _stored_previews(parsed.previews),
    )
    session.add(questionnaire)
    try:
        await session.flush()  # sends the INSERT, so the database assigns the id
    except IntegrityError:
        # Two uploads of the same file raced; the unique constraint let one through.
        await session.rollback()
        winner = await _by_hash(session, tenant_id, sha256)
        if winner is None:
            raise
        return Ingested(winner, created=False)

    session.add(
        QuestionnaireFile(
            questionnaire_id=questionnaire.id,
            tenant_id=tenant_id,
            content=content,
            size_bytes=len(content),
        )
    )
    session.add_all(_rows(parsed.questions, tenant_id=tenant_id, questionnaire_id=questionnaire.id))
    # File, questionnaire and questions are saved together or not at all.
    await session.commit()
    return Ingested(questionnaire, created=True)


async def _content(session: AsyncSession, questionnaire: Questionnaire) -> bytes:
    return (
        await session.execute(
            select(QuestionnaireFile.content).where(
                QuestionnaireFile.tenant_id == questionnaire.tenant_id,
                QuestionnaireFile.questionnaire_id == questionnaire.id,
            )
        )
    ).scalar_one()


async def apply_mapping(
    session: AsyncSession,
    questionnaire: Questionnaire,
    mapping: ManualMapping,
    settings: Settings,
) -> Questionnaire:
    """Parse the stored file with columns a person chose."""
    # Lock the row and re-read it, so two mappings submitted at once run one after the
    # other and the second sees the first one's result.
    await session.refresh(questionnaire, with_for_update=True)
    if questionnaire.status != STATUS_NEEDS_MAPPING:
        raise AlreadyParsedError("this questionnaire has already been parsed")

    parsed = await _parse_in_thread(await _content(session, questionnaire), settings, mapping)
    if not parsed.questions:
        raise MappingError(
            f"column {mapping.question_column.upper()} of sheet {mapping.sheet!r} has no text "
            f"below row {mapping.header_row}"
        )

    await session.execute(
        delete(Question).where(
            Question.tenant_id == questionnaire.tenant_id,
            Question.questionnaire_id == questionnaire.id,
        )
    )
    session.add_all(
        _rows(
            parsed.questions,
            tenant_id=questionnaire.tenant_id,
            questionnaire_id=questionnaire.id,
        )
    )
    questionnaire.status = STATUS_PARSED
    questionnaire.question_count = len(parsed.questions)
    questionnaire.mapping = parsed.mappings
    questionnaire.previews = None
    await session.commit()
    return questionnaire


async def page_of_questions(
    session: AsyncSession, questionnaire: Questionnaire, *, after: int, limit: int
) -> tuple[list[Question], int | None]:
    """Return up to `limit` questions after position `after`, and where the next page starts.

    Keyset paging: "position greater than the last one seen". Unlike OFFSET, the database
    never reads and throws away the rows of earlier pages.
    """
    rows = list(
        (
            await session.execute(
                select(Question)
                .where(
                    Question.tenant_id == questionnaire.tenant_id,
                    Question.questionnaire_id == questionnaire.id,
                    Question.position > after,
                )
                .order_by(Question.position)
                .limit(limit + 1)  # one extra row answers "is there another page?"
            )
        ).scalars()
    )
    if len(rows) > limit:
        return rows[:limit], rows[limit - 1].position
    return rows, None
