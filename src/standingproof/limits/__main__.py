"""The operator's switch and ceilings.

    python -m standingproof.limits show
    python -m standingproof.limits stop   --by NAME [--tenant TENANT_NAME]
    python -m standingproof.limits resume --by NAME [--tenant TENANT_NAME]
    python -m standingproof.limits daily  --by NAME --usd AMOUNT [--tenant TENANT_NAME]
    python -m standingproof.limits daily  --by NAME --default  [--tenant TENANT_NAME]

Without --tenant a command is about the whole deployment. `stop` takes effect at the next
model call and the next look at a live setting: neither is made. A question in progress
goes to a person with the reason; a question not yet started waits and goes on after
`resume`. Every change is written to the deployment's audit chain under the name in --by.
"""

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from standingproof.config import Settings, get_settings
from standingproof.db import Database, build_database
from standingproof.limits import switch
from standingproof.limits.guard import Guard
from standingproof.models import Tenant


async def _tenant(database: Database, name: str | None) -> uuid.UUID | None:
    if name is None:
        return None
    async with database.across_tenants() as session:
        found = (
            await session.execute(select(Tenant.id).where(Tenant.name == name))
        ).scalar_one_or_none()
    if found is None:
        raise SystemExit(f"no tenant is called {name!r}")
    return found


async def show(database: Database, guard: Guard, settings: Settings) -> list[str]:
    async with database.across_tenants() as session:
        tenants = (
            await session.execute(select(Tenant.name, Tenant.id).order_by(Tenant.name))
        ).all()
    lines = []
    everyone = Decimal(0)
    for name, tenant_id in tenants:
        async with database.tenant(tenant_id)() as session:
            state = await switch.read(session, tenant_id, settings)
        try:
            spent, everyone = await guard.spent_today(tenant_id)
            counted = f"${spent:.2f}"
        except (RedisError, OSError):
            # The switch is still worth showing when the counter cannot be read.
            counted, everyone = "unknown (the counter cannot be reached)", Decimal("NaN")
        lines.append(
            f"tenant {name}: {'STOPPED' if state.stopped else 'running'}, "
            f"{counted} of ${state.tenant_daily_usd:.2f} today"
        )
        deployment = state.deployment_daily_usd
    if tenants:
        lines.insert(0, f"deployment: ${everyone:.2f} of ${deployment:.2f} today (UTC)")
    return lines


async def run(
    database: Database, redis: Redis, settings: Settings, args: argparse.Namespace
) -> int:
    if args.command == "show":
        print("\n".join(await show(database, Guard(redis, database, settings), settings)))
        return 0
    tenant_id = await _tenant(database, args.tenant)
    async with database.across_tenants() as session:
        if args.command == "daily":
            await switch.set_daily_limit(session, tenant_id, args.usd, by=args.by)
        else:
            await switch.set_stopped(session, tenant_id, args.command == "stop", by=args.by)
    who = "the deployment" if tenant_id is None else f"tenant {args.tenant}"
    print(f"{args.command}: {who}, recorded under {args.by}")
    return 0


def _name(text: str) -> str:
    name = text.strip()
    if not 0 < len(name) <= 200:
        raise argparse.ArgumentTypeError("a name of 1 to 200 characters")
    return name


def _usd(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("an amount in dollars, such as 12.50") from None
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("an amount of zero or more")
    return value


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m standingproof.limits")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="the switch, the ceilings and today's spend")
    for name in ("stop", "resume", "daily"):
        command = commands.add_parser(name)
        command.add_argument("--by", required=True, type=_name, help="your name, for the audit log")
        command.add_argument("--tenant", help="a tenant's name; without it, the deployment")
        if name == "daily":
            amount = command.add_mutually_exclusive_group(required=True)
            amount.add_argument("--usd", type=_usd, help="the most to spend in one day")
            amount.add_argument(
                "--default", dest="usd", action="store_const", const=None, help="use the setting"
            )
    return top


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = build_database(settings)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_connect_timeout=2)
    try:
        return await run(database, redis, settings, args)
    finally:
        await redis.aclose()
        await database.dispose()


def main() -> None:
    sys.exit(asyncio.run(_main(parser().parse_args())))


if __name__ == "__main__":
    main()
