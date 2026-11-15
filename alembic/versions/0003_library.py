"""approved answers, policy documents and their chunks, and vectors per model

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (index suffix, model name, vector width). Written out here, not imported from the
# application: a migration must keep meaning what it meant on the day it was written.
_MODELS = [
    ("bge_small", "BAAI/bge-small-en-v1.5", 384),
    ("voyage_4_lite", "voyage-4-lite", 512),
]
_EMBEDDING_TABLES = ["library_answer_embeddings", "policy_chunk_embeddings"]


def _id() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column[object]:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "library_answers",
        _id(),
        _tenant(),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("control_id", sa.String(64), nullable=True),
        sa.Column(
            "search_text",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', question || ' ' || answer)", persisted=True),
        ),
        _created_at(),
    )
    op.create_index(
        "ix_library_answers_search_text", "library_answers", ["search_text"], postgresql_using="gin"
    )

    op.create_table(
        "policy_docs",
        _id(),
        _tenant(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_policy_docs_tenant_sha256"),
    )

    op.create_table(
        "policy_chunks",
        _id(),
        _tenant(),
        sa.Column(
            "policy_doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policy_docs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "search_text",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', heading_path || ' ' || text)", persisted=True),
        ),
        sa.UniqueConstraint("policy_doc_id", "position", name="uq_policy_chunks_position"),
    )
    op.create_index(
        "ix_policy_chunks_search_text", "policy_chunks", ["search_text"], postgresql_using="gin"
    )

    op.create_table(
        "library_answer_embeddings",
        sa.Column(
            "answer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("library_answers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.Text(), primary_key=True),
        _tenant(),
        sa.Column("embedding", Vector(), nullable=False),
    )
    op.create_table(
        "policy_chunk_embeddings",
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policy_chunks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.Text(), primary_key=True),
        _tenant(),
        sa.Column("embedding", Vector(), nullable=False),
    )

    # One approximate-nearest-neighbour index per model and table. The column holds vectors
    # of any width, so each index covers only its model's rows and casts them to that
    # model's width. A query must use the same cast and the same WHERE to use the index.
    for table in _EMBEDDING_TABLES:
        for suffix, model, width in _MODELS:
            op.execute(
                f"CREATE INDEX ix_{table}_{suffix} ON {table} "  # noqa: S608
                f"USING hnsw ((embedding::vector({width})) vector_cosine_ops) "
                f"WHERE model = '{model}'"
            )


def downgrade() -> None:
    for table in [*_EMBEDDING_TABLES, "policy_chunks", "policy_docs", "library_answers"]:
        op.drop_table(table)
