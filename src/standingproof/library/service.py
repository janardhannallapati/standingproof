"""Store approved answers and policy documents, each with its vector.

Text is embedded first and only then written, so the content and its vector are saved in
one transaction: there is never a stored entry that search cannot find. Every query filters
by tenant, and every change is committed here, before the caller builds a response.
"""

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.audit import Entry, record
from standingproof.config import Settings
from standingproof.library.chunking import TooManyChunksError, chunk_markdown
from standingproof.library.embedding import Embedder
from standingproof.models import (
    LibraryAnswer,
    LibraryAnswerEmbedding,
    PolicyChunk,
    PolicyChunkEmbedding,
    PolicyDoc,
)

_POLICY_SUFFIXES = {".md", ".markdown", ".txt"}
_MAX_TITLE_CHARS = 255
_DEFAULT_TITLE = "Untitled policy"


class UnsupportedPolicyError(Exception):
    """The upload is not a Markdown or plain-text file."""


class LibraryLimitError(Exception):
    """The content is empty or over a configured limit."""


@dataclass(frozen=True)
class NewAnswer:
    question: str
    answer: str
    owner: str
    expires_on: dt.date
    control_id: str | None


@dataclass(frozen=True)
class IngestedPolicy:
    doc: PolicyDoc
    created: bool


def answer_embedding_text(question: str, answer: str) -> str:
    return f"{question}\n{answer}"


def chunk_embedding_text(heading_path: str, text: str) -> str:
    # The headings say what the passage is about, which the passage itself often does not.
    return f"{heading_path}\n{text}" if heading_path else text


async def create_answer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    new: NewAnswer,
    embedder: Embedder,
    settings: Settings,
) -> LibraryAnswer:
    limit = settings.library_text_max_chars
    for name, value in (("question", new.question), ("answer", new.answer)):
        if len(value) > limit:
            raise LibraryLimitError(f"The {name} is longer than the {limit} character limit.")

    [vector] = await embedder.embed_documents([answer_embedding_text(new.question, new.answer)])

    answer = LibraryAnswer(
        tenant_id=tenant_id,
        question=new.question,
        answer=new.answer,
        owner=new.owner,
        expires_on=new.expires_on,
        control_id=new.control_id,
    )
    session.add(answer)
    await session.flush()  # sends the INSERT, so the database assigns the id
    session.add(
        LibraryAnswerEmbedding(
            answer_id=answer.id, model=embedder.spec.name, tenant_id=tenant_id, embedding=vector
        )
    )
    text_hash = hashlib.sha256(f"{new.question}\n{new.answer}".encode()).hexdigest()
    await record(
        session,
        tenant_id,
        Entry(
            action="library.answer.add",
            subject=f"answer:{answer.id}",
            details={"expires_on": new.expires_on.isoformat(), "text_sha256": text_hash},
        ),
    )
    await session.commit()
    return answer


async def get_answer(
    session: AsyncSession, tenant_id: uuid.UUID, answer_id: uuid.UUID
) -> LibraryAnswer | None:
    """Return the answer, or None if it does not exist *for this tenant*."""
    return (
        await session.execute(
            select(LibraryAnswer).where(
                LibraryAnswer.tenant_id == tenant_id, LibraryAnswer.id == answer_id
            )
        )
    ).scalar_one_or_none()


async def page_of_answers(
    session: AsyncSession, tenant_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
) -> list[LibraryAnswer]:
    """Up to `limit` answers whose id comes after `after`, in id order."""
    statement = select(LibraryAnswer).where(LibraryAnswer.tenant_id == tenant_id)
    if after is not None:
        statement = statement.where(LibraryAnswer.id > after)
    rows = await session.execute(statement.order_by(LibraryAnswer.id).limit(limit))
    return list(rows.scalars())


def _decode(filename: str | None, content: bytes) -> str:
    if PurePath(filename or "").suffix.lower() not in _POLICY_SUFFIXES:
        raise UnsupportedPolicyError("Only .md and .txt policy files are accepted.")
    try:
        decoded = content.decode("utf-8-sig")  # also drops a byte-order mark, if present
    except UnicodeDecodeError as error:
        raise UnsupportedPolicyError("The file is not UTF-8 text.") from error
    if "\x00" in decoded:
        raise UnsupportedPolicyError("The file is not text.")
    return decoded


def _title(decoded: str, filename: str | None) -> str:
    for line in decoded.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip().rstrip("#").strip()[:_MAX_TITLE_CHARS] or _DEFAULT_TITLE
    stem = PurePath(filename or "").stem.strip()
    return stem[:_MAX_TITLE_CHARS] or _DEFAULT_TITLE


async def _policy_by_hash(
    session: AsyncSession, tenant_id: uuid.UUID, sha256: str
) -> PolicyDoc | None:
    return (
        await session.execute(
            select(PolicyDoc).where(PolicyDoc.tenant_id == tenant_id, PolicyDoc.sha256 == sha256)
        )
    ).scalar_one_or_none()


async def ingest_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str | None,
    content: bytes,
    embedder: Embedder,
    settings: Settings,
) -> IngestedPolicy:
    """Split, embed and store one policy. Uploading the same bytes again changes nothing."""
    decoded = _decode(filename, content)
    sha256 = hashlib.sha256(content).hexdigest()
    existing = await _policy_by_hash(session, tenant_id, sha256)
    if existing is not None:
        return IngestedPolicy(existing, created=False)

    try:
        chunks = chunk_markdown(
            decoded, max_chars=settings.chunk_max_chars, max_chunks=settings.policy_max_chunks
        )
    except TooManyChunksError as error:
        raise LibraryLimitError(str(error)) from error
    if not chunks:
        raise LibraryLimitError("The file has no text to store.")

    vectors = await embedder.embed_documents(
        [chunk_embedding_text(chunk.heading_path, chunk.text) for chunk in chunks]
    )

    doc = PolicyDoc(
        tenant_id=tenant_id,
        title=_title(decoded, filename),
        sha256=sha256,
        chunk_count=len(chunks),
    )
    session.add(doc)
    try:
        await session.flush()
    except IntegrityError:
        # Two uploads of the same file raced; the unique constraint let one through.
        await session.rollback()
        winner = await _policy_by_hash(session, tenant_id, sha256)
        if winner is None:
            raise
        return IngestedPolicy(winner, created=False)

    rows = [
        PolicyChunk(
            tenant_id=tenant_id,
            policy_doc_id=doc.id,
            position=chunk.position,
            heading_path=chunk.heading_path,
            text=chunk.text,
        )
        for chunk in chunks
    ]
    session.add_all(rows)
    await session.flush()
    session.add_all(
        PolicyChunkEmbedding(
            chunk_id=row.id, model=embedder.spec.name, tenant_id=tenant_id, embedding=vector
        )
        for row, vector in zip(rows, vectors, strict=True)
    )
    await record(
        session,
        tenant_id,
        Entry(
            action="library.policy.add",
            subject=f"policy:{doc.id}",
            details={"sha256": doc.sha256, "chunks": len(rows)},
        ),
    )
    await session.commit()
    return IngestedPolicy(doc, created=True)


async def reembed_missing(
    sessions: async_sessionmaker[AsyncSession], embedder: Embedder, *, batch_size: int = 64
) -> int:
    """Give every entry that lacks one a vector from the active model. Returns how many.

    Run after switching the embedding model. Vectors from the previous model are kept, so
    switching back needs nothing.
    """
    model = embedder.spec.name
    total = 0
    async with sessions() as session:
        while True:
            answers = (
                (
                    await session.execute(
                        select(LibraryAnswer)
                        .where(
                            ~select(LibraryAnswerEmbedding.answer_id)
                            .where(
                                LibraryAnswerEmbedding.answer_id == LibraryAnswer.id,
                                LibraryAnswerEmbedding.model == model,
                            )
                            .exists()
                        )
                        .order_by(LibraryAnswer.id)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not answers:
                break
            vectors = await embedder.embed_documents(
                [answer_embedding_text(a.question, a.answer) for a in answers]
            )
            session.add_all(
                LibraryAnswerEmbedding(
                    answer_id=a.id, model=model, tenant_id=a.tenant_id, embedding=v
                )
                for a, v in zip(answers, vectors, strict=True)
            )
            await session.commit()
            total += len(answers)
        while True:
            chunks = (
                (
                    await session.execute(
                        select(PolicyChunk)
                        .where(
                            ~select(PolicyChunkEmbedding.chunk_id)
                            .where(
                                PolicyChunkEmbedding.chunk_id == PolicyChunk.id,
                                PolicyChunkEmbedding.model == model,
                            )
                            .exists()
                        )
                        .order_by(PolicyChunk.id)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not chunks:
                break
            vectors = await embedder.embed_documents(
                [chunk_embedding_text(c.heading_path, c.text) for c in chunks]
            )
            session.add_all(
                PolicyChunkEmbedding(chunk_id=c.id, model=model, tenant_id=c.tenant_id, embedding=v)
                for c, v in zip(chunks, vectors, strict=True)
            )
            await session.commit()
            total += len(chunks)
    return total
