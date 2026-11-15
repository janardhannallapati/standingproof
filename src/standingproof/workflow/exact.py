"""The shortcut before any model call: has the company already answered exactly this?"""

import uuid
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.models import LibraryAnswer
from standingproof.workflow.state import ExactMatch

# The same normalisation on both sides, done by the database: trimmed, spaces collapsed,
# lower case. Spaces are collapsed first, so a trailing line break is trimmed too. Wording
# that differs in any other way is a different question.
_NORMALISED = "lower(btrim(regexp_replace({}, '\\s+', ' ', 'g')))"


async def find_exact_answer(
    session: AsyncSession, *, tenant_id: uuid.UUID, question: str, today: date | None = None
) -> ExactMatch | None:
    """An approved, unexpired answer to exactly this question, if there is exactly one.

    Two approved answers to one question that say different things are a conflict for a
    person to settle, so neither is reused.
    """
    rows = (
        await session.execute(
            select(LibraryAnswer.id, LibraryAnswer.answer)
            .where(
                LibraryAnswer.tenant_id == tenant_id,
                LibraryAnswer.expires_on >= (today or date.today()),
                text(
                    f"{_NORMALISED.format('library_answers.question')} = "
                    f"{_NORMALISED.format('CAST(:asked AS text)')}"
                ).bindparams(asked=question),
            )
            .order_by(LibraryAnswer.created_at.desc())
            .limit(5)
        )
    ).all()
    if not rows or len({answer for _, answer in rows}) > 1:
        return None
    answer_id, answer = rows[0]
    return ExactMatch(source_id=f"answer:{answer_id}", answer=answer)
