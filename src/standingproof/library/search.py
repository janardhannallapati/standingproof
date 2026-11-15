"""One search over approved answers and policy chunks.

Two searches run over the same rows: a keyword search (Postgres full-text) and a meaning
search (vector distance). Their ranked lists are merged by position. Every query filters by
tenant, and an approved answer past its expiry date is never returned.
"""

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from standingproof.library.embedding import Embedder, ModelSpec
from standingproof.library.fusion import Fused, reciprocal_rank_fusion
from standingproof.models import LibraryAnswer, PolicyChunk, PolicyDoc

Kind = Literal["answer", "policy"]
SearchMode = Literal["keyword", "meaning"]
_Key = tuple[str, uuid.UUID]

BOTH_MODES: tuple[SearchMode, ...] = ("keyword", "meaning")
_CONTROL_ID = "control_id"
# Each search returns this many times the requested number, so the merge has material.
_DEPTH_FACTOR = 4
_MIN_DEPTH = 20
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")
_MAX_TOKENS = 50
# Keyword evidence counts for half. Postgres full-text ranking has no notion of a rare word
# against a common one, so a match on "data" scores like a match on "SAML"; its list is the
# noisier of the two. Measured in tests/eval: the exact value hardly matters (0.1 to 1.0
# gave the same recall), so this is a choice of principle, not a tuned number.
_WEIGHTS: dict[str, float] = {"keyword": 0.5}


@dataclass(frozen=True)
class SearchHit:
    kind: Kind
    id: uuid.UUID
    text: str  # the approved answer, or the policy text
    question: str | None  # answers only
    heading_path: str | None  # policy only
    document_title: str | None  # policy only
    owner: str | None
    expires_on: dt.date | None
    control_id: str | None
    found_by: tuple[str, ...]  # "keyword", "meaning", "control_id"
    score: float


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


# The words of the query joined with OR. plainto_tsquery treats its input as plain words
# (no operators), and joins them with AND; a question shares only some of its words with
# the right answer, so AND would find nothing. Ranking then favours rows matching more words.
_KEYWORD_SQL = text(
    """
    WITH q AS (
        SELECT replace(plainto_tsquery('english', :query)::text, '&', '|')::tsquery AS tsq
    )
    (SELECT 'answer' AS kind, a.id AS id, ts_rank_cd(a.search_text, q.tsq) AS score
       FROM library_answers a, q
      WHERE a.tenant_id = :tenant AND a.expires_on >= :today AND a.search_text @@ q.tsq)
    UNION ALL
    (SELECT 'policy', c.id, ts_rank_cd(c.search_text, q.tsq)
       FROM policy_chunks c, q
      WHERE c.tenant_id = :tenant AND c.search_text @@ q.tsq)
    ORDER BY score DESC, id
    LIMIT :depth
    """
)

_CONTROL_SQL = text(
    """
    SELECT id FROM library_answers
     WHERE tenant_id = :tenant AND expires_on >= :today AND lower(control_id) = ANY(:tokens)
     ORDER BY id
    """
)


def _meaning_sql(spec: ModelSpec) -> str:
    # The model name and width are written into the statement, not bound: Postgres uses a
    # partial index only when it can see the query's WHERE matches the index's WHERE. Both
    # come from the model registry in code (ModelSpec checks their form), never from a request.
    cast = f"vector({spec.dimensions})"
    distance = f"e.embedding::{cast} <=> CAST(:vector AS {cast})"
    return f"""
    (SELECT 'answer' AS kind, e.answer_id AS id, {distance} AS distance
       FROM library_answer_embeddings e
       JOIN library_answers a ON a.id = e.answer_id
      WHERE e.model = '{spec.name}' AND e.tenant_id = :tenant AND a.expires_on >= :today
      ORDER BY {distance}
      LIMIT :depth)
    UNION ALL
    (SELECT 'policy', e.chunk_id, {distance}
       FROM policy_chunk_embeddings e
      WHERE e.model = '{spec.name}' AND e.tenant_id = :tenant
      ORDER BY {distance}
      LIMIT :depth)
    ORDER BY distance, id
    LIMIT :depth
    """  # noqa: S608


async def _keyword(
    session: AsyncSession, tenant_id: uuid.UUID, query: str, today: dt.date, depth: int
) -> list[_Key]:
    rows = await session.execute(
        _KEYWORD_SQL, {"query": query, "tenant": tenant_id, "today": today, "depth": depth}
    )
    return [(row.kind, row.id) for row in rows]


async def _meaning(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vector: Sequence[float],
    spec: ModelSpec,
    today: dt.date,
    depth: int,
) -> list[_Key]:
    # The index walks nearest-first and the tenant filter is applied afterwards, so a plain
    # scan can stop with fewer rows than asked for. This makes it keep going until it has them.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = await session.execute(
        text(_meaning_sql(spec)),
        {"vector": str(list(vector)), "tenant": tenant_id, "today": today, "depth": depth},
    )
    return [
        (row.kind, row.id)
        for row in rows
        if spec.max_distance is None or row.distance <= spec.max_distance
    ]


async def _control_ids(
    session: AsyncSession, tenant_id: uuid.UUID, query: str, today: dt.date
) -> list[_Key]:
    tokens = sorted({t.rstrip(".").lower() for t in _TOKEN.findall(query)})[:_MAX_TOKENS]
    if not tokens:
        return []
    rows = await session.execute(
        _CONTROL_SQL, {"tenant": tenant_id, "today": today, "tokens": tokens}
    )
    return [("answer", row.id) for row in rows]


async def _load(
    session: AsyncSession, tenant_id: uuid.UUID, keys: Sequence[_Key]
) -> dict[_Key, dict[str, object]]:
    answer_ids = [key[1] for key in keys if key[0] == "answer"]
    chunk_ids = [key[1] for key in keys if key[0] == "policy"]
    found: dict[_Key, dict[str, object]] = {}
    if answer_ids:
        answers = await session.execute(
            select(LibraryAnswer).where(
                LibraryAnswer.tenant_id == tenant_id, LibraryAnswer.id.in_(answer_ids)
            )
        )
        for answer in answers.scalars():
            found[("answer", answer.id)] = {
                "text": answer.answer,
                "question": answer.question,
                "heading_path": None,
                "document_title": None,
                "owner": answer.owner,
                "expires_on": answer.expires_on,
                "control_id": answer.control_id,
            }
    if chunk_ids:
        chunks = await session.execute(
            select(PolicyChunk, PolicyDoc.title)
            .join(PolicyDoc, PolicyDoc.id == PolicyChunk.policy_doc_id)
            .where(PolicyChunk.tenant_id == tenant_id, PolicyChunk.id.in_(chunk_ids))
        )
        for chunk, title in chunks:
            found[("policy", chunk.id)] = {
                "text": chunk.text,
                "question": None,
                "heading_path": chunk.heading_path,
                "document_title": title,
                "owner": None,
                "expires_on": None,
                "control_id": None,
            }
    return found


def in_final_order(fused: Sequence[Fused[_Key]]) -> list[Fused[_Key]]:
    """Best score first. On equal scores an approved answer goes before policy text: it is
    the wording a person already signed off. Then by id, so the order never depends on what
    the database happened to return first."""
    return sorted(
        fused,
        key=lambda item: (-round(item.score, 12), item.key[0] != "answer", str(item.key[1])),
    )


async def search_library(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    embedder: Embedder,
    limit: int,
    today: dt.date | None = None,
    modes: Sequence[SearchMode] = BOTH_MODES,
) -> list[SearchHit]:
    """The best `limit` library entries for a question, most relevant first.

    `modes` exists for measuring: the retrieval evaluation runs keyword-only and
    meaning-only to show what each contributes. Normal callers leave it alone.
    """
    today = today or utc_today()
    depth = max(limit * _DEPTH_FACTOR, _MIN_DEPTH)

    rankings: dict[str, list[_Key]] = {}
    if "keyword" in modes:
        rankings["keyword"] = await _keyword(session, tenant_id, query, today, depth)
    if "meaning" in modes:
        vector = await embedder.embed_query(query)
        rankings["meaning"] = await _meaning(
            session, tenant_id, vector, embedder.spec, today, depth
        )
    fused = in_final_order(reciprocal_rank_fusion(rankings, weights=_WEIGHTS))

    # A control id typed exactly is a lookup, not a similarity question: it goes first.
    exact = await _control_ids(session, tenant_id, query, today)
    by_key = {item.key: item for item in fused}
    ordered: list[tuple[_Key, float, tuple[str, ...]]] = [
        (
            key,
            by_key[key].score if key in by_key else 0.0,
            (_CONTROL_ID, *(by_key[key].found_by if key in by_key else ())),
        )
        for key in exact
    ]
    ordered += [(item.key, item.score, item.found_by) for item in fused if item.key not in exact]
    ordered = ordered[:limit]

    rows = await _load(session, tenant_id, [key for key, _, _ in ordered])
    return [
        SearchHit(kind=key[0], id=key[1], found_by=found_by, score=score, **rows[key])  # type: ignore[arg-type]
        for key, score, found_by in ordered
        if key in rows
    ]
