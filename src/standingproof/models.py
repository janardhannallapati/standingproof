"""Database tables as typed classes.

Every table that holds customer data carries tenant_id, including child tables, so that
a tenant filter (and later a row-level security policy) never needs a join.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUS_PARSED = "parsed"
STATUS_NEEDS_MAPPING = "needs_mapping"

RUN_RUNNING = "running"
RUN_FINISHED = "finished"
RUN_CANCELLED = "cancelled"

# A question inside a run. The first two are in progress; the other four are final.
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_READY = "ready"
ITEM_NEEDS_REVIEW = "needs_review"
ITEM_FAILED = "failed"
ITEM_NOT_STARTED = "not_started"
ITEM_OPEN = (ITEM_QUEUED, ITEM_RUNNING)

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


class Connection(Base):
    """A scope in an outside system that this tenant may run checks against.

    Holds no credential. It is the allow-list the authorisation decision reads.
    """

    __tablename__ = "connections"
    __table_args__ = (
        # Across all tenants: the credentials are shared by the deployment, so a scope that two
        # tenants could both connect would let each read the other's settings.
        UniqueConstraint("system", "scope", name="uq_connections_scope"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    system: Mapped[str] = mapped_column(String(16))
    # A GitHub organisation (lower case) or a 12-digit AWS account id.
    scope: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class Evidence(Base):
    """What one run of a check saw, and when. Append-only: a new look is a new row."""

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_lookup", "tenant_id", "check_name", "scope", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    check_name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(300))
    # observed | unavailable | not_configured | denied
    status: Mapped[str] = mapped_column(String(16))
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    raw_result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    statement: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(Base):
    """One pass over a questionnaire: every question answered, or handed to a person."""

    __tablename__ = "runs"
    __table_args__ = (
        # Two runs at once would pay twice for the same answers.
        Index(
            "uq_runs_one_active",
            "questionnaire_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    questionnaire_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE")
    )
    # running | finished | cancelled
    status: Mapped[str] = mapped_column(String(16))
    # No further question starts once the run has spent this much.
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunQuestion(Base):
    """One question inside a run: where it is, who is working on it, and how it ended.

    Its id is also the thread id under which the workflow saves this question's state.
    """

    __tablename__ = "run_questions"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id", name="uq_run_questions_question"),
        Index("ix_run_questions_run_status", "run_id", "status"),
        # What the sweep looks for: work that is due and has nobody on it.
        Index(
            "ix_run_questions_open",
            "status",
            "enqueue_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    # queued | running | ready | needs_review | failed | not_started
    status: Mapped[str] = mapped_column(String(16))
    # reused (an approved answer matched exactly) | drafted (the model wrote it)
    path: Mapped[str | None] = mapped_column(String(16))
    # Why it needs review, failed or was never started.
    reason: Mapped[str | None] = mapped_column(String(40))
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[str] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), server_default=_ZERO)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # How many times a worker has taken this question up.
    attempts: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    # The worker holding this question, and until when. Only the holder may save a result.
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the sweep should next put this question on the queue if it is still open.
    enqueue_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the workflow's saved state for this question has been deleted.
    state_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
