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
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
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

# What a person has concluded about a question. Separate from its status, which is what
# the agent concluded. None: there is nothing to review (still open, failed, never started).
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_EDITED = "edited"
REVIEW_REJECTED = "rejected"
REVIEW_REDRAFTING = "redrafting"
REVIEW_EXPIRED = "expired"

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
    # The free-text cell beside the answer, when the sheet has a comments column.
    comment_cell: Mapped[str | None] = mapped_column(String(16))
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
    __table_args__ = (Index("ix_evidence_lookup", "tenant_id", "check_name", "scope", "seq"),)

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
    # When the application's clock said it looked. For people to read, not for ordering.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The order rows were stored in, from a database sequence. "Newest" means highest seq:
    # a clock can be stepped back, a sequence cannot.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))


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
    # pending | approved | edited | rejected | redrafting | expired, or NULL.
    review: Mapped[str | None] = mapped_column(String(16))
    # The wording a reviewer approved. `answer` stays what the model drafted.
    final_answer: Mapped[str | None] = mapped_column(Text)
    # Goes up on every change a reviewer could see. A decision names the version it saw.
    version: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    redrafts: Mapped[int] = mapped_column(Integer, server_default=_ZERO)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the workflow's saved state for this question has been deleted.
    state_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a live setting this answer rests on was seen to have changed, and cleared by
    # the next decision. Separate from `status` (the agent) and `review` (a person).
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewDecision(Base):
    """One decision by a person about one question. Append-only: the record of who approved
    which wording, and when. The latest row for a question is its current decision."""

    __tablename__ = "review_decisions"
    __table_args__ = (Index("ix_review_decisions_question", "run_question_id", "decided_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    run_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_questions.id", ondelete="CASCADE")
    )
    # approve | edit | reject | redraft
    decision: Mapped[str] = mapped_column(String(16))
    # The draft the reviewer was looking at when they decided.
    shown_answer: Mapped[str | None] = mapped_column(Text)
    # For approve and edit: the wording that was approved.
    final_answer: Mapped[str | None] = mapped_column(Text)
    # For reject: why. For redraft: the instruction to the agent.
    comment: Mapped[str | None] = mapped_column(Text)
    # True when approved together with others, without being opened.
    batch: Mapped[bool] = mapped_column(Boolean, server_default=_FALSE)
    decided_by: Mapped[str] = mapped_column(String(200))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class DraftCheck(Base):
    """What the independent checker made of one wording. Append-only.

    A check is evidence about the exact text in `answer_checked` and about nothing else:
    a redraft gets a new row, and a reviewer's edit has none. The latest row for a
    question is its current check.
    """

    __tablename__ = "draft_checks"
    __table_args__ = (Index("ix_draft_checks_question", "run_question_id", "checked_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    run_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_questions.id", ondelete="CASCADE")
    )
    answer_checked: Mapped[str] = mapped_column(Text)
    # agrees | disagrees | not_checked
    outcome: Mapped[str] = mapped_column(String(16))
    # A list of {kind, quote, located, source_id, explanation}. Model output: show as text.
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # None when no call was made (the question was at its limit).
    model: Mapped[str | None] = mapped_column(String(100))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), server_default=_ZERO)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class FilledFile(Base):
    """The uploaded workbook with approved answers written into it. Append-only.

    `key` names the exact input: the upload, every question's decision and wording, and the
    writer's version. The same input is written once; asking again finds this row. A changed
    decision is a different key and a new row, and the earlier file stays.
    """

    __tablename__ = "filled_files"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_filled_files_run_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    # [{position, sheet, cell}] and [{position, sheet, cell, reason, detail}].
    written: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    left_empty: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    writer_version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class SentFile(Base):
    """A reviewer's record that a filled file was sent to a customer. Append-only.

    It says a person recorded it as sent; it does not say it was delivered. `answers` holds,
    per written question, the hash of the wording that went out, so a later change to a
    setting can be traced to what each customer was told.
    """

    __tablename__ = "sent_files"
    __table_args__ = (
        # Recording the same file for the same customer twice is one record.
        Index(
            "uq_sent_files_file_customer", "filled_file_id", text("lower(customer)"), unique=True
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    filled_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("filled_files.id", ondelete="CASCADE")
    )
    customer: Mapped[str] = mapped_column(String(200))
    # [{question_id, position, wording_sha256}]
    answers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    sent_by: Mapped[str] = mapped_column(String(200))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class AnswerEvidence(Base):
    """Which observation an answer rests on, per setting. The one drift table that is
    updated in place: it points at the evidence the answer cited, or was last reopened
    against. Its history is in `answer_reopenings`."""

    __tablename__ = "answer_evidence"
    __table_args__ = (
        # "Which answers rest on this setting?" is the question a new observation asks.
        Index("ix_answer_evidence_setting", "tenant_id", "check_name", "scope"),
    )

    run_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_questions.id", ondelete="CASCADE"), primary_key=True
    )
    check_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id", ondelete="CASCADE"))


class DriftQueue(Base):
    """An observation nobody has compared with the answers yet. Written in the same
    transaction as its evidence and deleted in the one that examines its setting, so an
    observation is never lost between the two and never examined by halves."""

    __tablename__ = "drift_queue"

    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    # Why the setting was looked at: run | schedule | webhook | manual
    trigger: Mapped[str] = mapped_column(String(16))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class DriftEvent(Base):
    """One observation that differed from the one before it, or from what an answer
    rested on. Append-only."""

    __tablename__ = "drift_events"
    __table_args__ = (Index("ix_drift_events_tenant", "tenant_id", "detected_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    check_name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(300))
    # The observation that differed. One event per observation, however often it is examined.
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), unique=True
    )
    # The observation before it, when that one differs. None: only answers differed.
    previous_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class AnswerReopening(Base):
    """One answer put back in front of a reviewer by one drift event. Append-only."""

    __tablename__ = "answer_reopenings"
    __table_args__ = (
        UniqueConstraint("drift_event_id", "run_question_id", name="uq_answer_reopenings_once"),
        Index("ix_answer_reopenings_question", "run_question_id", "reopened_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    drift_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("drift_events.id", ondelete="CASCADE")
    )
    run_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_questions.id", ondelete="CASCADE")
    )
    # What the answer rested on until now.
    was_evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE")
    )
    # [{fact, was, now}]
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # The review state it had, so that "keep" can put it back. None if it was already
    # waiting when this event arrived.
    was_review: Mapped[str | None] = mapped_column(String(16))
    reopened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class WebhookDelivery(Base):
    """One delivery from an outside system that passed its signature check. Append-only
    but for `handled_at` and `outcome`. The payload is not kept: only where to look."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # A delivery sent twice (a redelivery, a retry, a replay) is one row.
        UniqueConstraint("source", "delivery_id", name="uq_webhook_deliveries_delivery"),
        Index(
            "ix_webhook_deliveries_waiting",
            "received_at",
            postgresql_where=text("handled_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    source: Mapped[str] = mapped_column(String(16))
    delivery_id: Mapped[str] = mapped_column(String(100))
    event: Mapped[str] = mapped_column(String(60))
    action: Mapped[str | None] = mapped_column(String(40))
    # None when no tenant has connected the organisation the delivery is about.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    # The setting to look at. None when the delivery is ignored.
    check_name: Mapped[str | None] = mapped_column(String(100))
    scope: Mapped[str | None] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # looked | ignored:<reason>
    outcome: Mapped[str | None] = mapped_column(String(60))


class AuditEntry(Base):
    """One entry of a tenant's audit chain (or the deployment's, when tenant_id is NULL).
    Append-only, by grant and by trigger. See standingproof.audit.chain."""

    __tablename__ = "audit_log"
    __table_args__ = (
        # NULLS NOT DISTINCT: the deployment's chain has one entry per number too.
        UniqueConstraint(
            "tenant_id", "seq", name="uq_audit_log_seq", postgresql_nulls_not_distinct=True
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    # No ON DELETE CASCADE: a tenant with a record cannot be deleted from under it.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"))
    # 1, 2, 3... within the chain, with no gaps.
    seq: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(60))
    subject: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
