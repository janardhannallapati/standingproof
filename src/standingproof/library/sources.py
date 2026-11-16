"""Turn a source id from a citation into the text it points to, for one tenant.

A draft cites ids: `answer:<uuid>`, `policy:<uuid>`, `evidence:<uuid>`. A reviewer needs
the words. An id that is malformed, unknown, or another tenant's resolves to None, with
nothing to tell those cases apart.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.checks.service import source_text
from standingproof.models import Evidence, LibraryAnswer, PolicyChunk, PolicyDoc


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # answer | policy | evidence
    # What the answer may rely on.
    text: str
    # One line saying where it comes from.
    label: str
    owner: str | None = None
    valid_until: date | None = None
    observed_at: datetime | None = None


async def resolve(session: AsyncSession, tenant_id: uuid.UUID, source_id: str) -> Source | None:
    kind, separator, raw = source_id.partition(":")
    if not separator:
        return None
    try:
        row_id = uuid.UUID(raw)
    except ValueError:
        return None

    if kind == "answer":
        answer = (
            await session.execute(
                select(LibraryAnswer).where(
                    LibraryAnswer.tenant_id == tenant_id, LibraryAnswer.id == row_id
                )
            )
        ).scalar_one_or_none()
        if answer is None:
            return None
        return Source(
            id=source_id,
            kind=kind,
            text=answer.answer,
            label=answer.question,
            owner=answer.owner,
            valid_until=answer.expires_on,
        )
    if kind == "policy":
        found = (
            await session.execute(
                select(PolicyChunk, PolicyDoc.title)
                .join(PolicyDoc, PolicyDoc.id == PolicyChunk.policy_doc_id)
                .where(PolicyChunk.tenant_id == tenant_id, PolicyChunk.id == row_id)
            )
        ).one_or_none()
        if found is None:
            return None
        chunk, title = found
        return Source(
            id=source_id, kind=kind, text=chunk.text, label=f"{title} > {chunk.heading_path}"
        )
    if kind == "evidence":
        evidence = (
            await session.execute(
                select(Evidence).where(
                    Evidence.tenant_id == tenant_id,
                    Evidence.id == row_id,
                    # Only something that was seen can be a source.
                    Evidence.status == "observed",
                )
            )
        ).scalar_one_or_none()
        if evidence is None:
            return None
        return Source(
            id=source_id,
            kind=kind,
            text=source_text(evidence),
            label=f"{evidence.check_name} on {evidence.scope}",
            observed_at=evidence.observed_at,
        )
    return None


async def resolve_all(
    session: AsyncSession, tenant_id: uuid.UUID, source_ids: list[str]
) -> list[Source | None]:
    """Each id's source, in order. None where one can no longer be found."""
    return [await resolve(session, tenant_id, source_id) for source_id in source_ids]
