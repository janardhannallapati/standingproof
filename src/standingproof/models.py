"""Database tables as typed classes.

Every table that holds customer data carries tenant_id, including child tables, so that
a tenant filter (and later a row-level security policy) never needs a join.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUS_PARSED = "parsed"
STATUS_NEEDS_MAPPING = "needs_mapping"

_UUID_DEFAULT = text("gen_random_uuid()")
_NOW = text("now()")
_ZERO = text("0")
# Defined here, not inline: inside Question the name `text` is the column, not this function.
_FALSE = text("false")


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class Questionnaire(Base):
    __tablename__ = "questionnaires"
    __table_args__ = (
        # The same bytes uploaded twice by one tenant are one questionnaire.
        UniqueConstraint("tenant_id", "sha256", name="uq_questionnaires_tenant_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    question_count: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    # Sheet name -> the header row and columns the questions were read from.
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The top-left corner of each sheet, kept only while a person must choose the columns,
    # so that showing it never means parsing the file again.
    previews: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class QuestionnaireFile(Base):
    """The uploaded bytes, unchanged. A separate table so listings never load them."""

    __tablename__ = "questionnaire_files"

    questionnaire_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("questionnaire_id", "position", name="uq_questions_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    questionnaire_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE")
    )
    # Order within the questionnaire: sheet order, then row order. Starts at 1.
    position: Mapped[int] = mapped_column(Integer)
    sheet: Mapped[str] = mapped_column(Text)
    cell: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    answer_cell: Mapped[str | None] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(255))
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=_FALSE)


class LibraryAnswer(Base):
    """An answer the company has approved, with who owns it and when it must be reviewed."""

    __tablename__ = "library_answers"
    __table_args__ = (
        Index("ix_library_answers_search_text", "search_text", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    # From the day after this date the answer is no longer offered as a source.
    expires_on: Mapped[date] = mapped_column(Date)
    control_id: Mapped[str | None] = mapped_column(String(64))
    # Kept by the database from question and answer, so it cannot fall out of step with them.
    search_text: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', question || ' ' || answer)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class PolicyDoc(Base):
    __tablename__ = "policy_docs"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="uq_policy_docs_tenant_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class PolicyChunk(Base):
    """One piece of a policy document: small enough to embed, and to quote as a source."""

    __tablename__ = "policy_chunks"
    __table_args__ = (
        UniqueConstraint("policy_doc_id", "position", name="uq_policy_chunks_position"),
        Index("ix_policy_chunks_search_text", "search_text", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    policy_doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_docs.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    # The headings above this piece, for example "Access Control > 4.2 Authentication".
    heading_path: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    search_text: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', heading_path || ' ' || text)", persisted=True),
    )


# Vectors live beside the content, one row per (item, model). The column has no fixed
# width because models differ; each model has its own partial index, cast to its width
# (see the migrations). So a second model can be tried without touching the content tables.


class LibraryAnswerEmbedding(Base):
    __tablename__ = "library_answer_embeddings"

    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_answers.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    embedding: Mapped[Any] = mapped_column(Vector())


class PolicyChunkEmbedding(Base):
    __tablename__ = "policy_chunk_embeddings"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    embedding: Mapped[Any] = mapped_column(Vector())
