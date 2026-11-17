"""Check the audit chains.

    python -m standingproof.audit verify    recompute every chain; exit 1 if one is broken
    python -m standingproof.audit head      print each chain's last entry number and hash

`verify` shows that no single entry was changed, removed or reordered. To also show that
a chain was not rebuilt or cut short, keep the output of `head` somewhere the database's
owner cannot reach, and compare it with a later one: an earlier head must still be an
entry of the later chain.
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from standingproof.audit.chain import head, verify
from standingproof.config import get_settings
from standingproof.db import Database, build_database
from standingproof.models import Tenant

DEPLOYMENT = "deployment"


async def _chains(database: Database) -> list[tuple[str, uuid.UUID | None]]:
    # Which tenants there are is asked across tenants. Each chain is then read as its own
    # tenant; the deployment's chain belongs to the system role.
    async with database.across_tenants() as session:
        rows = (await session.execute(select(Tenant.name, Tenant.id).order_by(Tenant.name))).all()
    return [(DEPLOYMENT, None), *((f"tenant {name}", tenant_id) for name, tenant_id in rows)]


async def run(database: Database, command: str) -> int:
    """Print one line per chain. Returns the process's exit code."""
    broken = 0
    for label, tenant_id in await _chains(database):
        sessions = database.across_tenants if tenant_id is None else database.tenant(tenant_id)
        async with sessions() as session:
            if command == "head":
                seq, digest = await head(session, tenant_id)
                print(f"{label}: entry {seq}, hash {digest}")
                continue
            try:
                sound, problem = await verify(session, tenant_id)
            except Exception as error:  # noqa: BLE001 - one unreadable chain must not hide the rest
                broken += 1
                print(
                    f"{label}: BROKEN: could not be read ({type(error).__name__})", file=sys.stderr
                )
                continue
        if problem is None:
            print(f"{label}: {sound} entries, chain intact")
        else:
            broken += 1
            print(
                f"{label}: BROKEN at entry {problem.seq}: {problem.reason} "
                f"({problem.bad} of {sound + problem.bad} entries do not verify)",
                file=sys.stderr,
            )
    return 1 if broken else 0


async def _main(command: str) -> int:
    database = build_database(get_settings())
    try:
        return await run(database, command)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m standingproof.audit")
    parser.add_argument("command", choices=["verify", "head"])
    sys.exit(asyncio.run(_main(parser.parse_args().command)))


if __name__ == "__main__":
    main()
