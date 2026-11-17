"""Connections to Postgres, and which tenant each one acts for.

The running processes use two roles. `standingproof_app` is subject to row-level security:
it sees and changes only the rows of the tenant named in the transaction-local setting
`app.tenant_id`, and nothing at all when none is named. `standingproof_system` finds work
across tenants (which question is due, which setting to look at) and has no access to the
tables that hold a customer's documents and files. See SECURITY.md.
"""

import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from standingproof.config import Settings

APP_ROLE = "standingproof_app"
SYSTEM_ROLE = "standingproof_system"
STATE_ROLE = "standingproof_state"
ROLES = {"app": APP_ROLE, "system": SYSTEM_ROLE, "state": STATE_ROLE}

_TENANT = "tenant_id"
# Who a session acts for, for the audit log. Set beside the tenant; absent means "system".
ACTOR = "actor"
# The key audit entries are signed with, when there is one. It stays in the process: it is
# never sent to the database, so nobody who can only run SQL can sign an entry.
AUDIT_KEY = "audit_key"


class UnsafeRoleError(RuntimeError):
    """A process was about to serve requests as a role the row-level rules do not bind."""


@event.listens_for(Session, "after_begin")
def _name_the_tenant(session: Session, transaction: Any, connection: Connection) -> None:
    """Tell the database which tenant this transaction acts for.

    Runs at the start of every transaction of a session, so it holds after a commit in
    the middle of a session too. The setting is local to the transaction: when it ends the
    connection goes back to the pool naming no tenant.
    """
    tenant_id = session.info.get(_TENANT)
    if tenant_id is not None:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)}
        )


def build_engine(database_url: str, **pool: Any) -> AsyncEngine:
    """Create a connection pool.

    pool_pre_ping tests a pooled connection before handing it out, so a connection the
    database has closed is replaced instead of failing a request. The connect timeout keeps
    a dead database from hanging callers.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 2},
        **pool,
    )


def build_session_factory(engine: AsyncEngine, **info: object) -> async_sessionmaker[AsyncSession]:
    """Create the factory that hands out sessions.

    expire_on_commit=False keeps loaded objects readable after commit. The default would
    reload each attribute on next access, which in async code is a hidden database call.
    """
    return async_sessionmaker(engine, expire_on_commit=False, info=info)


_UNSAFE = """
    SELECT r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
           OR r.rolname <> '{role}'
           -- It owns one of our tables, or is a member of a role that does.
           OR EXISTS (SELECT 1 FROM pg_tables t
                      WHERE t.schemaname IN ('public', 'workflow')
                        AND pg_has_role(r.oid, t.tableowner, 'MEMBER'))
           -- It is a member, directly or through another role, of any role that is not on
           -- the short list below: it would have that role's rights. So not of another
           -- running role, of the owner, or of pg_read_all_data and its like.
           OR EXISTS (SELECT 1 FROM pg_roles o
                      WHERE o.oid <> r.oid AND pg_has_role(r.oid, o.oid, 'MEMBER')
                        AND o.rolname NOT IN ({tolerated}))
    FROM pg_roles r WHERE r.rolname = current_user
"""


# Memberships that give no right on our tables and that a managed database may require:
# reading server statistics, and signing in through the provider's identity service.
TOLERATED_MEMBERSHIPS = (
    "pg_monitor",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_stat_scan_tables",
    "rds_iam",
)


def unsafe_role_query(role: str) -> str:
    """SQL that is true when the signed-in role is not exactly `role`, with its limits:
    a superuser, exempt from row-level security, an owner of our tables, or a member of
    any role outside TOLERATED_MEMBERSHIPS. `role` is one of our three constants."""
    if role not in ROLES.values():
        raise ValueError("not one of the running roles")
    tolerated = ", ".join(f"'{name}'" for name in TOLERATED_MEMBERSHIPS)
    return _UNSAFE.format(role=role, tolerated=tolerated)


def refusal(role: str) -> UnsafeRoleError:
    return UnsafeRoleError(
        f"this database login is not {role} with that role's limits (it is another role, a "
        "superuser, exempt from row-level security, an owner of the tables, or a member of "
        "a role other than a monitoring or sign-in role); refusing to use it"
    )


def _refuse_unsafe_role(engine: AsyncEngine, role: str) -> None:
    """Check each new connection of a pool, as it is made.

    Not once at start: a process must be able to start while the database is away, and
    the URL it was given is only proved wrong by a connection that succeeds.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def check(dbapi_connection: Any, record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(unsafe_role_query(role))
            (unsafe,) = cursor.fetchone()
        finally:
            cursor.close()
        if unsafe:
            raise refusal(role)


class Database:
    """The two pools a running process uses, and the sessions it may open on them."""

    def __init__(self, *, app_url: str, system_url: str, audit_key: bytes | None = None) -> None:
        self.app_engine = build_engine(app_url)
        self._system_engine = build_engine(system_url)
        _refuse_unsafe_role(self.app_engine, APP_ROLE)
        _refuse_unsafe_role(self._system_engine, SYSTEM_ROLE)
        self._audit_key = audit_key
        self._system = build_session_factory(self._system_engine, **{AUDIT_KEY: audit_key})

    def tenant(
        self, tenant_id: uuid.UUID, *, actor: str | None = None
    ) -> async_sessionmaker[AsyncSession]:
        """Sessions that see and change one tenant's rows, whatever the query says.

        `actor` is the person the session acts for, recorded with what they decide."""
        return build_session_factory(
            self.app_engine, **{_TENANT: tenant_id, ACTOR: actor, AUDIT_KEY: self._audit_key}
        )

    @property
    def across_tenants(self) -> async_sessionmaker[AsyncSession]:
        """Sessions as the system role, which no tenant binds. Only for finding work and
        housekeeping: a test lists the modules that may use this
        (tests/attacks/test_cross_tenant_modules.py)."""
        return self._system

    async def dispose(self) -> None:
        await self.app_engine.dispose()
        await self._system_engine.dispose()


def build_database(settings: Settings) -> Database:
    key = settings.audit_key
    return Database(
        app_url=settings.role_url("app"),
        system_url=settings.role_url("system"),
        audit_key=None if key is None else key.get_secret_value().encode(),
    )
