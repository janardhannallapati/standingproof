"""The agent's two read-only tools, backed by the database library.

Same tool names and inputs as the in-memory tools of `agent/library.py`. A source id says
what it points at: `answer:<uuid>` or `policy:<uuid>`.
"""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.agent.library import PolicyInput, SearchInput
from standingproof.agent.tools import Tool, ToolOutcome, ToolRegistry
from standingproof.library.embedding import Embedder, EmbeddingError
from standingproof.library.search import SearchHit, search_library
from standingproof.models import PolicyChunk, PolicyDoc

_ANSWER = "answer"
_POLICY = "policy"


def _source_id(hit: SearchHit) -> str:
    return f"{hit.kind}:{hit.id}"


def _entry(hit: SearchHit) -> dict[str, object]:
    if hit.kind == _ANSWER:
        return {
            "id": _source_id(hit),
            "question": hit.question,
            "approved_answer": hit.text,
            "owner": hit.owner,
            "valid_until": hit.expires_on.isoformat() if hit.expires_on else None,
            "control_id": hit.control_id,
        }
    return {
        "id": _source_id(hit),
        "document": hit.document_title,
        "section": hit.heading_path,
        "policy_text": hit.text,
    }


def build_library_registry(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    embedder: Embedder,
    *,
    limit: int = 5,
) -> ToolRegistry:
    """Tools for one run, bound to one tenant. The model cannot name another tenant."""

    async def search(arguments: SearchInput) -> ToolOutcome:
        try:
            async with sessions() as session:
                hits = await search_library(
                    session,
                    tenant_id=tenant_id,
                    query=arguments.query,
                    embedder=embedder,
                    limit=limit,
                )
        except EmbeddingError as error:
            return ToolOutcome(content=f"Error: search is unavailable ({error}).", is_error=True)
        if not hits:
            return ToolOutcome(
                content="No approved answer or policy text matches that query. Try different "
                "words, or report that there is no approved source."
            )
        return ToolOutcome(
            content=json.dumps([_entry(hit) for hit in hits]),
            sources={_source_id(hit): hit.text for hit in hits},
        )

    async def policy(arguments: PolicyInput) -> ToolOutcome:
        kind, _, raw_id = arguments.source_id.partition(":")
        try:
            chunk_id = uuid.UUID(raw_id) if kind == _POLICY else None
        except ValueError:
            chunk_id = None
        row = None
        if chunk_id is not None:
            async with sessions() as session:
                row = (
                    await session.execute(
                        select(PolicyChunk, PolicyDoc.title)
                        .join(PolicyDoc, PolicyDoc.id == PolicyChunk.policy_doc_id)
                        .where(PolicyChunk.tenant_id == tenant_id, PolicyChunk.id == chunk_id)
                    )
                ).one_or_none()
        if row is None:
            return ToolOutcome(
                content=f"Error: no policy section with id {arguments.source_id!r}. Use an id "
                "beginning 'policy:' that search_approved_answers returned.",
                is_error=True,
            )
        chunk, title = row
        return ToolOutcome(
            content=json.dumps(
                {
                    "id": arguments.source_id,
                    "document": title,
                    "section": chunk.heading_path,
                    "text": chunk.text,
                }
            ),
            sources={arguments.source_id: chunk.text},
        )

    return ToolRegistry(
        [
            Tool(
                name="search_approved_answers",
                description=(
                    "Search the company's approved questionnaire answers and policy text. "
                    "Matches by wording and by meaning, so a plain description of the control "
                    "works. Returns entries with their id; approved answers carry their owner "
                    "and the date they are valid until. Call this first for every question."
                ),
                input_model=SearchInput,
                handler=search,
                read_only=True,
            ),
            Tool(
                name="get_policy_section",
                description=(
                    "Fetch one policy section in full, by an id beginning 'policy:' that "
                    "search_approved_answers returned. Use it when the question asks for "
                    "detail the approved answer does not give."
                ),
                input_model=PolicyInput,
                handler=policy,
                read_only=True,
            ),
        ]
    )
