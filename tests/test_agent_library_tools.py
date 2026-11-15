"""The agent's two tools reading the database library instead of the in-memory list."""

import asyncio
import json
import uuid
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.agent.library import build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import run_agent
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import AnswerDraft, ToolCall
from standingproof.db import build_engine, build_session_factory
from standingproof.library.service import reembed_missing
from standingproof.library.tools import build_library_registry
from standingproof.models import Tenant
from tests.agent.fakes import ScriptedModel, finishes, search, wants_tools
from tests.conftest import ClientFactory
from tests.fakes import FakeEmbedder
from tests.test_library_api import MFA, YESTERDAY, add_answer, count, upload_policy


class Db:
    """One event loop and one connection pool for a test, both closed afterwards."""

    def __init__(self, database_url: str) -> None:
        # One loop for the whole test: a pool belongs to the loop it was first used on.
        self.loop = asyncio.new_event_loop()
        self._engine = build_engine(database_url)
        self.sessions: async_sessionmaker[AsyncSession] = build_session_factory(self._engine)

    def run[T](self, awaitable: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(awaitable)

    def tenant(self, name: str = "default") -> uuid.UUID:
        async def lookup() -> uuid.UUID:
            async with self.sessions() as session:
                statement = select(Tenant.id).where(Tenant.name == name)
                return (await session.execute(statement)).scalar_one()

        return self.run(lookup())

    def registry(self, name: str = "default") -> ToolRegistry:
        return build_library_registry(self.sessions, self.tenant(name), FakeEmbedder())

    def close(self) -> None:
        self.run(self._engine.dispose())
        self.loop.close()


@pytest.fixture
def db(migrated_postgres_url: str) -> Iterator[Db]:
    database = Db(migrated_postgres_url)
    yield database
    database.close()


def policy_call(call_id: str, source_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="get_policy_section", arguments={"source_id": source_id})


def test_the_tools_keep_the_names_and_inputs_the_model_was_given_in_step_two(
    api: TestClient, db: Db
) -> None:
    api.get("/library/answers")  # creates the development tenant
    old = {d["name"]: d["input_schema"] for d in build_registry([]).definitions()}
    new = {d["name"]: d["input_schema"] for d in db.registry().definitions()}

    assert new == old


def test_search_returns_database_entries_as_sources(api: TestClient, db: Db) -> None:
    answer_id = add_answer(api).json()["id"]
    upload_policy(api)
    registry = db.registry()

    outcome = db.run(registry.execute(search("c1", "2FA for staff and employees")))

    assert outcome.is_error is False
    entries = json.loads(outcome.content)
    assert entries[0]["id"] == f"answer:{answer_id}"
    assert entries[0]["approved_answer"] == MFA["answer"]
    assert entries[0]["owner"] == MFA["owner"]
    policy = next(e for e in entries if e["id"].startswith("policy:"))
    assert policy["section"] == "Access Control Policy > 4.2 Authentication"
    # Every id the model may cite is a source, with the exact text it will be checked against.
    assert set(outcome.sources) == {e["id"] for e in entries}
    assert outcome.sources[f"answer:{answer_id}"] == MFA["answer"]


def test_search_with_no_match_says_so_and_offers_no_source(api: TestClient, db: Db) -> None:
    add_answer(api, expires_on=YESTERDAY)
    registry = db.registry()

    outcome = db.run(registry.execute(search("c1", "multi-factor employees")))

    assert outcome.sources == {}
    assert "No approved" in outcome.content


def test_get_policy_section_returns_the_passage_for_an_id_search_gave(
    api: TestClient, db: Db
) -> None:
    upload_policy(api)
    registry = db.registry()
    found = db.run(registry.execute(search("c1", "second factor workforce")))
    source_id = json.loads(found.content)[0]["id"]

    outcome = db.run(registry.execute(policy_call("c2", source_id)))

    assert outcome.is_error is False
    assert json.loads(outcome.content)["text"] == (
        "All workforce members must authenticate with a second factor."
    )
    assert list(outcome.sources) == [source_id]


def test_get_policy_section_refuses_unknown_malformed_and_foreign_ids(
    make_api: ClientFactory, db: Db
) -> None:
    ours = make_api()
    theirs = make_api(dev_tenant_name="another-company")
    ours.get("/library/answers")
    upload_policy(theirs)
    their_registry = db.registry("another-company")
    found = db.run(their_registry.execute(search("c1", "second factor")))
    their_source = json.loads(found.content)[0]["id"]
    registry = db.registry()

    for source_id in [their_source, f"policy:{uuid.uuid4()}", "policy:not-a-uuid", "LIB-MFA-01"]:
        outcome = db.run(registry.execute(policy_call("c2", source_id)))
        assert outcome.is_error is True, source_id
        assert outcome.sources == {}


def test_a_whole_run_answers_from_the_database_library(api: TestClient, db: Db) -> None:
    answer_id = add_answer(api).json()["id"]
    source = f"answer:{answer_id}"
    draft = AnswerDraft(status="answered", answer=str(MFA["answer"]), citations=[source], note="")
    model = ScriptedModel([wants_tools(search("c1", "MFA employees")), finishes(draft)])

    result = db.run(
        run_agent(
            question="Is MFA enforced for employees?",
            model=model,
            registry=db.registry(),
            limits=Limits(),
        )
    )

    assert result.handoff is None
    assert result.draft is not None
    assert result.draft.citations == [source]


def test_reembed_gives_existing_rows_vectors_from_a_newly_chosen_model(
    api: TestClient, db: Db, migrated_postgres_url: str
) -> None:
    from standingproof.library.embedding import MODELS

    add_answer(api)
    upload_policy(api)  # two chunks
    voyage = FakeEmbedder(MODELS["voyage-4-lite"])

    first = db.run(reembed_missing(db.sessions, voyage, batch_size=2))
    second = db.run(reembed_missing(db.sessions, voyage, batch_size=2))

    assert (first, second) == (3, 0)
    new = "model = 'voyage-4-lite' AND vector_dims(embedding) = 512"
    old = "model = 'BAAI/bge-small-en-v1.5'"
    assert count(migrated_postgres_url, f"library_answer_embeddings WHERE {new}") == 1
    assert count(migrated_postgres_url, f"policy_chunk_embeddings WHERE {new}") == 2
    # The first model's vectors are still there, so switching back needs nothing.
    assert count(migrated_postgres_url, f"library_answer_embeddings WHERE {old}") == 1
