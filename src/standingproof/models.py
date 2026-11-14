"""Database tables as typed classes.

Every table that holds customer data carries tenant_id, including child tables, so that
a tenant filter (and later a row-level security policy) never needs a join.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
