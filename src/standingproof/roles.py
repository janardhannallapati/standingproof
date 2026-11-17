"""Give the three running roles their passwords.

    python -m standingproof.roles

Run as the owner, after `alembic upgrade head` (which creates the roles and their rights)
and on every deploy: it is how a password is changed. Each role's password is the one in
its own URL (SP_APP_DATABASE_URL, SP_SYSTEM_DATABASE_URL, SP_STATE_DATABASE_URL).

The password is sent inside an `ALTER ROLE` statement, so a server that logs statements
(`log_statement = ddl` or `all`) writes it to its log.

On a managed database you may prefer to set the passwords yourself and never run this:
`ALTER ROLE standingproof_app LOGIN PASSWORD '...'`, and the same for the other two.
"""

import asyncio
from typing import Literal, get_args

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from standingproof.config import Settings, get_settings
from standingproof.db import ROLES, build_engine

Role = Literal["app", "system", "state"]


_THIS_MACHINE = {"localhost", "127.0.0.1", "::1"}


def _refuse_guessable_passwords(settings: Settings) -> None:
    """A role URL that was not given is derived, with the role's name as its password.
    That is for a database on this machine. Anywhere else each URL must be given."""
    host = make_url(settings.database_url.get_secret_value()).host
    if host in _THIS_MACHINE:
        return
    for role in get_args(Role):
        if getattr(settings, f"{role}_database_url") is None:
            raise ValueError(
                f"SP_{role.upper()}_DATABASE_URL must be set: the database is not on this "
                "machine, and a role whose password is its own name must not exist there"
            )


async def ensure_logins(settings: Settings) -> None:
    _refuse_guessable_passwords(settings)
    # hide_parameters: an error here must not print a password into a log.
    engine = build_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as conn:
            for role in get_args(Role):
                url = make_url(settings.role_url(role))
                if url.username != ROLES[role]:
                    raise ValueError(
                        f"the {role} database URL must sign in as {ROLES[role]}: "
                        "the rights in the migrations are given to that name"
                    )
                # ALTER ROLE takes no parameters, so the database quotes the values itself.
                statement = (
                    await conn.execute(
                        text(
                            "SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', "
                            "CAST(:name AS text), CAST(:password AS text))"
                        ),
                        {"name": url.username, "password": url.password or ""},
                    )
                ).scalar_one()
                try:
                    await conn.exec_driver_sql(statement)
                except DBAPIError as error:
                    # Not chained: the driver's message quotes the statement, password and all.
                    raise RuntimeError(
                        f"could not set the password of {url.username}: {type(error.orig).__name__}"
                    ) from None
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(ensure_logins(get_settings()))
    print("The app, system and state roles can sign in.")


if __name__ == "__main__":
    main()
