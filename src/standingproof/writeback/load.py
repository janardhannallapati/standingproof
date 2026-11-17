"""A run's questions with what a reviewer decided, as the writer needs them.

Also the two things derived from them and from nothing else: the key that names a file's
exact input, and the name a download is given.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import (
    ITEM_OPEN,
    REVIEW_APPROVED,
    REVIEW_EDITED,
    REVIEW_PENDING,
    REVIEW_REDRAFTING,
    Question,
    Questionnaire,
    QuestionnaireFile,
    ReviewDecision,
    Run,
    RunQuestion,
)
from standingproof.writeback.plan import Item

# Goes into the key. Raise it when the writer would produce different bytes for the same
# input, so that files made by an older writer are not mistaken for current ones.
WRITER_VERSION = 2
_WRITTEN = (REVIEW_APPROVED, REVIEW_EDITED)
_SAFE_IN_A_NAME = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- "


@dataclass(frozen=True)
class Row:
    """One question of the run with what a reviewer decided."""

    question_id: uuid.UUID
    position: int
    sheet: str
    cell: str | None
    question: str
    status: str
    review: str | None
    wording: str | None
    comment: str | None
    # False when the decision record does not carry this wording.
    as_decided: bool
    # The free-text cell beside the answer cell, when the sheet has a comments column.
    comment_cell: str | None = None

    @property
    def undecided(self) -> bool:
        return self.status in ITEM_OPEN or self.review in (REVIEW_PENDING, REVIEW_REDRAFTING)

    def item(self) -> Item:
        # A question that failed or never started has no decision and no answer: it is
        # closed, and its cell is left empty.
        review = self.review if self.review is not None else "unanswered"
        return Item(self.position, self.sheet, self.cell, review, self.wording, self.comment_cell)


@dataclass(frozen=True)
class Loaded:
    run: Run
    filename: str
    upload_sha256: str
    content: bytes
    rows: list[Row]


def filled_name(uploaded: str) -> str:
    """The download's name: the upload's name, cleaned, with `-filled` before `.xlsx`.

    The upload's name was chosen by whoever sent the file and goes into a response
    header, so only letters, digits and `._- ` are kept.
    """
    base = uploaded.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".xlsx"):
        base = base[: -len(".xlsx")]
    kept = "".join(ch for ch in base if ch in _SAFE_IN_A_NAME).strip(" .")
    return f"{kept or 'questionnaire'}-filled.xlsx"


def file_key(upload_sha256: str, rows: Sequence[Row]) -> str:
    """The same upload, decisions and wording always give the same key."""
    described = [
        [
            str(row.question_id),
            row.status,
            row.review or "",
            # Where it would be written: a different mapping is a different file.
            row.cell or "",
            row.comment_cell or "",
            hashlib.sha256((row.wording or "").encode()).hexdigest(),
        ]
        for row in sorted(rows, key=lambda each: each.position)
    ]
    payload = {"writer": WRITER_VERSION, "upload": upload_sha256, "questions": described}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def load(session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID) -> Loaded | None:
    found = (
        await session.execute(
            select(Run, Questionnaire.filename, Questionnaire.sha256, QuestionnaireFile.content)
            .join(Questionnaire, Questionnaire.id == Run.questionnaire_id)
            .join(QuestionnaireFile, QuestionnaireFile.questionnaire_id == Questionnaire.id)
            .where(Run.id == run_id, Run.tenant_id == tenant_id)
        )
    ).one_or_none()
    if found is None:
        return None
    run, filename, upload_sha256, content = found

    questions = (
        await session.execute(
            select(RunQuestion, Question)
            .join(Question, Question.id == RunQuestion.question_id)
            .where(RunQuestion.run_id == run_id, RunQuestion.tenant_id == tenant_id)
            .order_by(RunQuestion.position)
        )
    ).all()
    decisions = (
        await session.execute(
            select(ReviewDecision)
            .join(RunQuestion, RunQuestion.id == ReviewDecision.run_question_id)
            .where(RunQuestion.run_id == run_id, ReviewDecision.tenant_id == tenant_id)
            # In stored order, so the last one kept below is the decision stored last.
            .order_by(ReviewDecision.seq)
        )
    ).scalars()
    latest: dict[uuid.UUID, ReviewDecision] = {d.run_question_id: d for d in decisions}

    rows: list[Row] = []
    for item, question in questions:
        decision = latest.get(item.id)
        written = item.review in _WRITTEN
        # What goes into the file must be the wording a decision recorded. For an approval
        # that is also the wording the reviewer was shown.
        as_decided = not written or (
            decision is not None
            and decision.final_answer == item.final_answer
            and (item.review == REVIEW_EDITED or decision.shown_answer == item.final_answer)
        )
        rows.append(
            Row(
                question_id=question.id,
                position=item.position,
                sheet=question.sheet,
                cell=question.answer_cell,
                question=question.text,
                status=item.status,
                review=item.review,
                wording=item.final_answer if written else None,
                comment=decision.comment if decision is not None else None,
                as_decided=as_decided,
                comment_cell=question.comment_cell,
            )
        )
    return Loaded(run, filename, upload_sha256, content, rows)
