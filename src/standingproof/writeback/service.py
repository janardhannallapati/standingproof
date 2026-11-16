"""The filled file, joined to storage: previewed, made once, and handed back.

"Once" rests on two things. The file's key is computed from everything that decides its
content (the upload, each question's decision and wording, the writer's version), so the
same input always has the same key. And the key is unique per run in the database, so a
second attempt, at the same moment or after a crash, finds the first result.

The file and its record are written in one transaction. There is no state in which a file
exists and its record does not, or the other way round.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import FilledFile, Questionnaire, Run
from standingproof.writeback.load import WRITER_VERSION, Row, file_key, filled_name, load
from standingproof.writeback.plan import Plan, plan_writeback
from standingproof.writeback.writer import read_indexes, verify_unchanged, write_cells


class NotAllDecidedError(Exception):
    def __init__(self, undecided: int) -> None:
        super().__init__(f"{undecided} questions still need a decision")
        self.undecided = undecided


class WordingMismatchError(Exception):
    """A question's approved wording is not the wording its decision recorded."""

    def __init__(self, positions: Sequence[int]) -> None:
        listed = ", ".join(f"question {position}" for position in positions)
        super().__init__(f"the approved wording changed after it was decided: {listed}")
        self.positions = list(positions)


@dataclass(frozen=True)
class FileInfo:
    id: uuid.UUID
    sha256: str
    size_bytes: int
    created_at: datetime
    created_by: str
    # True when this file was made from the decisions as they are now.
    current: bool


@dataclass(frozen=True)
class Preview:
    filename: str
    rows: list[Row]
    plan: Plan
    protected_sheets: list[str]
    latest: FileInfo | None

    @property
    def undecided(self) -> int:
        return sum(row.undecided for row in self.rows)


def _plan(content: bytes, rows: Sequence[Row]) -> tuple[Plan, list[str]]:
    """Runs in a thread: it reads the workbook's XML."""
    indexes = read_indexes(content, {row.sheet for row in rows})
    plan = plan_writeback([row.item() for row in rows], indexes)
    return plan, sorted(name for name, index in indexes.items() if index.protected)


async def _latest(session: AsyncSession, run_id: uuid.UUID, key: str) -> FileInfo | None:
    # The columns are named so that the file's bytes are not loaded for a listing.
    row = (
        await session.execute(
            select(
                FilledFile.id,
                FilledFile.sha256,
                FilledFile.size_bytes,
                FilledFile.created_at,
                FilledFile.created_by,
                FilledFile.key,
            )
            .where(FilledFile.run_id == run_id)
            .order_by(FilledFile.created_at.desc(), FilledFile.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    return FileInfo(
        row.id, row.sha256, row.size_bytes, row.created_at, row.created_by, row.key == key
    )


async def preview(session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID) -> Preview | None:
    """What a file made now would contain. Stores nothing."""
    loaded = await load(session, tenant_id, run_id)
    if loaded is None:
        return None
    plan, protected = await asyncio.to_thread(_plan, loaded.content, loaded.rows)
    latest = await _latest(session, run_id, file_key(loaded.upload_sha256, loaded.rows))
    return Preview(filled_name(loaded.filename), loaded.rows, plan, protected, latest)


def _build(content: bytes, rows: Sequence[Row]) -> tuple[bytes, Plan]:
    """Runs in a thread: plan, write, and check the result before anyone sees it."""
    plan, _ = _plan(content, rows)
    filled = write_cells(content, plan.writes)
    verify_unchanged(content, filled, plan.writes)
    return filled, plan


async def make_file(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID, *, created_by: str
) -> tuple[FileInfo, bool] | None:
    """Make the file for the decisions as they are, or find the one already made.

    Returns the file and whether this call made it. Raises NotAllDecidedError,
    WordingMismatchError, or a WritebackError when the workbook cannot be written safely.
    """
    loaded = await load(session, tenant_id, run_id)
    if loaded is None:
        return None
    undecided = sum(row.undecided for row in loaded.rows)
    if undecided:
        raise NotAllDecidedError(undecided)
    changed = [row.position for row in loaded.rows if not row.as_decided]
    if changed:
        raise WordingMismatchError(changed)

    key = file_key(loaded.upload_sha256, loaded.rows)
    existing = await _latest(session, run_id, key)
    if existing is not None and existing.current:
        return existing, False

    filled, plan = await asyncio.to_thread(_build, loaded.content, loaded.rows)
    inserted = (
        await session.execute(
            insert(FilledFile)
            .values(
                tenant_id=tenant_id,
                run_id=run_id,
                key=key,
                content=filled,
                size_bytes=len(filled),
                sha256=hashlib.sha256(filled).hexdigest(),
                written=[
                    {"position": w.position, "sheet": w.sheet, "cell": w.cell} for w in plan.writes
                ],
                left_empty=[
                    {
                        "position": s.position,
                        "sheet": s.sheet,
                        "cell": s.cell,
                        "reason": s.reason,
                        "detail": s.detail,
                    }
                    for s in plan.skips
                ],
                writer_version=WRITER_VERSION,
                created_by=created_by,
            )
            # Someone else made it between our look and our insert: theirs stands.
            .on_conflict_do_nothing(constraint="uq_filled_files_run_key")
            .returning(FilledFile.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    stored = await _by_key(session, run_id, key)
    return stored, inserted is not None


async def _by_key(session: AsyncSession, run_id: uuid.UUID, key: str) -> FileInfo:
    row = (
        await session.execute(
            select(
                FilledFile.id,
                FilledFile.sha256,
                FilledFile.size_bytes,
                FilledFile.created_at,
                FilledFile.created_by,
            ).where(FilledFile.run_id == run_id, FilledFile.key == key)
        )
    ).one()
    return FileInfo(row.id, row.sha256, row.size_bytes, row.created_at, row.created_by, True)


async def read_file(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID, file_id: uuid.UUID
) -> tuple[str, bytes] | None:
    """The download's name and bytes, or None if it is not this tenant's file of this run."""
    row = (
        await session.execute(
            select(Questionnaire.filename, FilledFile.content)
            .join(Run, Run.id == FilledFile.run_id)
            .join(Questionnaire, Questionnaire.id == Run.questionnaire_id)
            .where(
                FilledFile.id == file_id,
                FilledFile.run_id == run_id,
                FilledFile.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return filled_name(row.filename), row.content
