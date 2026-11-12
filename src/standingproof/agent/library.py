"""A small in-memory answer library and the two tools that read it.

This stands in for the database-backed library that a later step adds. The tools' names,
inputs and outputs stay the same when that happens.
"""

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from standingproof.agent.tools import Tool, ToolOutcome, ToolRegistry


@dataclass(frozen=True)
class LibraryEntry:
    id: str
    topic: str
    approved_answer: str
    policy_excerpt: str
    approved_on: str


LIBRARY: list[LibraryEntry] = [
    LibraryEntry(
        id="LIB-MFA-01",
        topic="multi-factor authentication MFA 2FA two-factor login employees",
        approved_answer=(
            "Yes. Multi-factor authentication is required for all employee accounts "
            "through our identity provider."
        ),
        policy_excerpt=(
            "Access Control Policy 4.2: All workforce members must authenticate with a "
            "second factor. SMS is not an accepted factor."
        ),
        approved_on="2026-09-01",
    ),
    LibraryEntry(
        id="LIB-ENC-01",
        topic="encryption at rest data storage database disks AES",
        approved_answer="Yes. Customer data is encrypted at rest using AES-256.",
        policy_excerpt=(
            "Cryptography Policy 2.1: Production data stores must enable provider-managed "
            "encryption at rest with AES-256."
        ),
        approved_on="2026-08-14",
    ),
    LibraryEntry(
        id="LIB-BCP-01",
        topic="business continuity disaster recovery DR test backup restore",
        approved_answer=(
            "We test our disaster recovery plan annually. The last test was in March 2026."
        ),
        policy_excerpt=(
            "Business Continuity Policy 3.4: The recovery plan is exercised at least once "
            "per calendar year and results are reviewed by the security lead."
        ),
        approved_on="2026-04-02",
    ),
]


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Keywords describing the control, for example 'MFA'.")


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="An id returned by search_approved_answers.")


def build_registry(library: list[LibraryEntry]) -> ToolRegistry:
    by_id = {entry.id: entry for entry in library}

    async def search(arguments: SearchInput) -> ToolOutcome:
        words = [w for w in arguments.query.lower().split() if len(w) > 2]
        hits = [e for e in library if any(w in e.topic.lower() for w in words)]
        if not hits:
            return ToolOutcome(
                content="No approved answer matches that query. Try different keywords, "
                "or report that there is no approved source."
            )
        payload = [
            {"id": e.id, "approved_answer": e.approved_answer, "approved_on": e.approved_on}
            for e in hits
        ]
        return ToolOutcome(content=json.dumps(payload), source_ids=frozenset(e.id for e in hits))

    async def policy(arguments: PolicyInput) -> ToolOutcome:
        entry = by_id.get(arguments.source_id)
        if entry is None:
            return ToolOutcome(
                content=f"Error: no source with id {arguments.source_id!r}. "
                "Use an id returned by search_approved_answers.",
                is_error=True,
            )
        return ToolOutcome(
            content=json.dumps({"id": entry.id, "policy_excerpt": entry.policy_excerpt}),
            source_ids=frozenset({entry.id}),
        )

    return ToolRegistry(
        [
            Tool(
                name="search_approved_answers",
                description=(
                    "Search the company's approved questionnaire answers by keyword. "
                    "Returns matching entries with their id, approved wording and approval "
                    "date. Call this first for every question."
                ),
                input_model=SearchInput,
                handler=search,
                read_only=True,
            ),
            Tool(
                name="get_policy_section",
                description=(
                    "Fetch the policy text behind an approved answer, by the id that "
                    "search_approved_answers returned. Use it when the question asks for "
                    "detail the approved answer does not give."
                ),
                input_model=PolicyInput,
                handler=policy,
                read_only=True,
            ),
        ]
    )
