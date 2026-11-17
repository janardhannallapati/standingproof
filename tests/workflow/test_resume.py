"""Saved state in a real Postgres: stop a question part way, continue it elsewhere."""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.types import AnswerDraft, ModelTurn
from standingproof.config import get_settings
from standingproof.workflow.checkpoint import (
    SCHEMA,
    conninfo,
    delete_state,
    open_checkpointer,
    setup,
)
from standingproof.workflow.graph import build_graph, run_question
from standingproof.workflow.state import ExactMatch, Outcome, QuestionContext
from tests.agent.fakes import ScriptedModel, finishes, search, wants_tools
from tests.conftest import settings_for

MFA_ID = "LIB-MFA-01"
QUESTION = "Is multi-factor authentication enforced for all employees?"
ANSWERED = AnswerDraft(
    status="answered", answer="Yes. MFA is required.", citations=[MFA_ID], note=""
)


class Killed(BaseException):
    """Stands in for the process dying: not an Exception, so nothing in the way catches it."""


class DiesAfter(ScriptedModel):
    """Answers its scripted turns, then dies on the next call."""

    async def complete(self, **kwargs: Any) -> ModelTurn:
        if not self._turns:
            raise Killed
        return await super().complete(**kwargs)


async def no_match(question: str) -> ExactMatch | None:
    return None


def context(model: Any, **overrides: Any) -> QuestionContext:
    values: dict[str, Any] = {
        "model": model,
        "registry": build_registry(LIBRARY),
        "limits": Limits(),
        "find_exact": no_match,
        **overrides,
    }
    return QuestionContext(**values)


@pytest.fixture(scope="module")
def database(migrated_postgres_url: str) -> str:
    asyncio.run(setup(migrated_postgres_url))
    return migrated_postgres_url


async def one_worker(database: str, thread: str, ctx: QuestionContext) -> Outcome:
    """Everything a fresh process would build: its own pool, saver and graph."""
    async with open_checkpointer(settings_for(database).role_url("state")) as saver:
        return await run_question(
            build_graph(saver), thread_id=thread, question=QUESTION, context=ctx
        )


def _tables(database: str, schema: str) -> set[str]:
    with psycopg.connect(conninfo(database)) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
        ).fetchall()
    return {row[0] for row in rows}


def _saved_rows(database: str, thread: str) -> int:
    with psycopg.connect(conninfo(database)) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.checkpoints WHERE thread_id = %s",  # noqa: S608
            (thread,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_library_url_is_the_application_url_without_the_driver() -> None:
    assert conninfo("postgresql+asyncpg://u:p@h:5/db") == "postgresql://u:p@h:5/db"
    assert conninfo("postgresql://u:p@h:5/db") == "postgresql://u:p@h:5/db"
    with pytest.raises(ValueError, match="scheme"):
        conninfo("not-a-url")


def test_setup_puts_the_tables_in_their_own_schema_and_can_run_twice(database: str) -> None:
    asyncio.run(setup(database))

    assert "checkpoints" in _tables(database, SCHEMA)
    assert not any(name.startswith("checkpoint") for name in _tables(database, "public"))


# A plain `def`: Alembic's env.py calls asyncio.run itself.
def test_our_migrations_do_not_see_the_library_tables(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SP_DATABASE_URL", database)
    get_settings.cache_clear()
    try:
        command.check(Config("alembic.ini"))
    finally:
        get_settings.cache_clear()


@pytest.fixture
def thread() -> Iterator[str]:
    yield str(uuid.uuid4())


async def test_a_question_stopped_after_a_model_call_continues_without_repeating_it(
    database: str, thread: str
) -> None:
    first = DiesAfter([wants_tools(search("c1", "MFA"))])
    with pytest.raises(Killed):
        await one_worker(database, thread, context(first))
    assert len(first.requests) == 1

    second = ScriptedModel([finishes(ANSWERED)])
    outcome = await one_worker(database, thread, context(second))

    # Only the remaining turn was asked for, and it saw the tool result from before the stop.
    assert len(second.requests) == 1
    messages = second.requests[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert MFA_ID in messages[-1]["content"][0]["content"]
    assert outcome.status == "ready"
    assert outcome.steps == 2


async def test_the_resumed_result_equals_an_uninterrupted_one(database: str, thread: str) -> None:
    turns = [wants_tools(search("c1", "MFA")), finishes(ANSWERED)]
    whole = await one_worker(database, str(uuid.uuid4()), context(ScriptedModel(turns)))

    with pytest.raises(Killed):
        await one_worker(database, thread, context(DiesAfter(turns[:1])))
    resumed = await one_worker(database, thread, context(ScriptedModel(turns[1:])))

    assert resumed == whole


async def test_a_question_stopped_while_running_tools_runs_them_again_once(
    database: str, thread: str
) -> None:
    class DyingRegistry:
        def __init__(self) -> None:
            self._inner = build_registry(LIBRARY)

        def definitions(self) -> list[dict[str, Any]]:
            return self._inner.definitions()

        async def execute(self, call: Any) -> Any:
            raise Killed

    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(ANSWERED)])
    with pytest.raises(Killed):
        await one_worker(database, thread, context(model, registry=DyingRegistry()))

    outcome = await one_worker(database, thread, context(model))

    # The model turn before the stop was saved; the tool step ran again from its start.
    assert len(model.requests) == 2
    tool_message = model.requests[1]["messages"][-1]
    assert len(tool_message["content"]) == 1
    assert outcome.status == "ready"


async def test_a_finished_question_is_answered_from_saved_state(database: str, thread: str) -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(ANSWERED)])
    first = await one_worker(database, thread, context(model))

    again = await one_worker(database, thread, context(ScriptedModel([])))

    assert again == first


async def test_deleting_saved_state_removes_every_row_for_the_question(
    database: str, thread: str
) -> None:
    model = ScriptedModel([wants_tools(search("c1", "MFA")), finishes(ANSWERED)])
    await one_worker(database, thread, context(model))
    assert _saved_rows(database, thread) > 0

    async with open_checkpointer(settings_for(database).role_url("state")) as saver:
        await delete_state(saver, [thread])

    assert _saved_rows(database, thread) == 0
