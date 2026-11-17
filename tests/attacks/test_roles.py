"""The roles the processes sign in as: refused when unsafe, and kept apart."""

import asyncio
import uuid

import pytest
from psycopg import AsyncConnection, errors
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from standingproof.config import Settings
from standingproof.db import Database, UnsafeRoleError
from standingproof.roles import ensure_logins
from standingproof.workflow.checkpoint import conninfo, open_checkpointer, setup
from tests.conftest import Db, settings_for
from tests.workflow.seed import wf

__all__ = ["wf"]


def test_a_process_refuses_to_use_the_owners_login(wf: Db) -> None:
    # What a deployment gets wrong: every URL set to the one login it already had.
    as_owner = Database(app_url=wf.url, system_url=wf.url)

    async def go() -> None:
        try:
            for sessions in (as_owner.tenant(uuid.uuid4()), as_owner.across_tenants):
                with pytest.raises((UnsafeRoleError, DBAPIError), match="refusing to use it"):
                    async with sessions() as session:
                        await session.execute(text("SELECT 1"))
        finally:
            await as_owner.dispose()

    wf.run(go())


def test_a_role_is_refused_where_another_roles_login_was_given(wf: Db) -> None:
    settings = settings_for(wf.url)
    # The system role's login as the app's: "tenant" sessions would then cross tenants.
    swapped = Database(app_url=settings.role_url("system"), system_url=settings.role_url("app"))

    async def go() -> None:
        try:
            for sessions in (swapped.tenant(uuid.uuid4()), swapped.across_tenants):
                with pytest.raises((UnsafeRoleError, DBAPIError), match="refusing to use it"):
                    async with sessions() as session:
                        await session.execute(text("SELECT 1"))
        finally:
            await swapped.dispose()

    wf.run(go())


@pytest.mark.parametrize("login", ["owner", "app"])
def test_saved_state_is_kept_under_the_state_role_only(
    wf: Db, login: str, caplog: pytest.LogCaptureFixture
) -> None:
    url = wf.url if login == "owner" else settings_for(wf.url).role_url("app")

    async def go() -> None:
        await setup(wf.url)
        with pytest.raises(Exception, match="pool initialization incomplete"):
            async with open_checkpointer(url, max_connections=1, wait_seconds=3):
                pass

    wf.run(go())

    # The pool reports only that it could not open. Why is in its log: our refusal, not
    # a wrong address or a slow start.
    assert "refusing to use it" in caplog.text


def test_role_name_passwords_are_set_only_on_this_machine() -> None:
    elsewhere = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://owner:secret@db.internal:5432/standingproof"),
    )

    with pytest.raises(ValueError, match="SP_APP_DATABASE_URL"):
        asyncio.run(ensure_logins(elsewhere))


def test_a_role_with_an_ordinary_extra_membership_is_still_accepted(wf: Db) -> None:
    # A managed database asks for memberships of its own (sign-in by IAM, monitoring).
    # Those give no right on our tables and must not lock the application out.
    async def go() -> list[object]:
        async with wf.sessions() as session:
            await session.execute(text("GRANT pg_monitor TO standingproof_app"))
            await session.commit()
        fresh = Database(
            app_url=settings_for(wf.url).role_url("app"),
            system_url=settings_for(wf.url).role_url("system"),
        )
        try:
            async with fresh.tenant(uuid.uuid4())() as session:
                return list((await session.execute(text("SELECT 1"))).scalars())
        finally:
            await fresh.dispose()
            async with wf.sessions() as session:
                await session.execute(text("REVOKE pg_monitor FROM standingproof_app"))
                await session.commit()

    assert wf.run(go()) == [1]


def test_a_role_that_is_a_member_of_another_running_role_is_refused(wf: Db) -> None:
    async def go() -> None:
        async with wf.sessions() as session:
            await session.execute(text("GRANT standingproof_system TO standingproof_app"))
            await session.commit()
        fresh = Database(
            app_url=settings_for(wf.url).role_url("app"),
            system_url=settings_for(wf.url).role_url("system"),
        )
        try:
            with pytest.raises((UnsafeRoleError, DBAPIError), match="refusing to use it"):
                async with fresh.tenant(uuid.uuid4())() as session:
                    await session.execute(text("SELECT 1"))
        finally:
            await fresh.dispose()
            async with wf.sessions() as session:
                await session.execute(text("REVOKE standingproof_system FROM standingproof_app"))
                await session.commit()

    wf.run(go())


def test_in_production_each_role_must_be_given_its_own_url() -> None:
    settings = Settings(_env_file=None, environment="prod")

    for role in ("app", "system", "state"):
        with pytest.raises(ValueError, match=f"SP_{role.upper()}_DATABASE_URL"):
            settings.role_url(role)  # type: ignore[arg-type]


def test_a_role_url_must_sign_in_as_the_role_the_rights_were_given_to(wf: Db) -> None:
    wrong = settings_for(wf.url, app_database_url=SecretStr(wf.url))

    with pytest.raises(ValueError, match="standingproof_app"):
        asyncio.run(ensure_logins(wrong))


def test_a_failure_to_set_a_password_does_not_print_it(wf: Db) -> None:
    settings = settings_for(wf.url)
    secret = "hunter2-do-not-print"  # noqa: S105 - made up, to look for in the message
    app = settings.role_url("app").replace(":standingproof_app@", f":{secret}@")
    # The owner's login is replaced by one that may not alter roles.
    weak = settings_for(settings.role_url("system"), app_database_url=SecretStr(app))

    with pytest.raises(RuntimeError) as failed:
        asyncio.run(ensure_logins(weak))

    assert secret not in str(failed.value) and failed.value.__cause__ is None


def test_saved_state_and_customer_tables_are_kept_by_different_roles(wf: Db) -> None:
    settings = settings_for(wf.url)
    state_url = settings.role_url("state")

    async def go() -> None:
        await setup(wf.url)
        # The state role does its own work...
        async with open_checkpointer(state_url, max_connections=1) as saver:
            assert await saver.aget_tuple({"configurable": {"thread_id": "nobody"}}) is None
        # ...and cannot read a customer table, or be used to set the rules aside.
        async with await AsyncConnection.connect(conninfo(state_url), autocommit=True) as conn:
            for statement in (
                "SELECT 1 FROM public.questionnaires",
                "SELECT 1 FROM public.run_questions",
                "CREATE TABLE workflow.loot (id int)",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    await conn.execute(statement)
        # The app and system roles cannot read saved state.
        for sessions in (wf.database.tenant(uuid.uuid4()), wf.database.across_tenants):
            async with sessions() as session:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await session.execute(text("SELECT 1 FROM workflow.checkpoints"))

    wf.run(go())
