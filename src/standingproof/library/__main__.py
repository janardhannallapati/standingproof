"""Library maintenance commands.

    python -m standingproof.library reembed

Gives every stored entry that lacks one a vector from the embedding model named in the
settings. Run it once after changing SP_EMBEDDING_MODEL.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from standingproof.config import get_settings
from standingproof.db import build_database
from standingproof.library.embedding import EmbeddingConfigError, EmbeddingError, build_embedder
from standingproof.library.service import reembed_missing
from standingproof.models import Tenant


async def _reembed() -> int:
    settings = get_settings()
    try:
        embedder = build_embedder(settings)
    except EmbeddingConfigError as error:
        print(error, file=sys.stderr)
        return 2
    database = build_database(settings)
    count = 0
    try:
        # Which tenants there are is asked across tenants. Each library is then read and
        # written as its own tenant.
        async with database.across_tenants() as session:
            tenants = list((await session.execute(select(Tenant.id))).scalars())
        for tenant_id in tenants:
            count += await reembed_missing(database.tenant(tenant_id), embedder)
    except EmbeddingError as error:
        print(f"Stopped: {error} Entries already embedded are kept; run again.", file=sys.stderr)
        return 1
    finally:
        await embedder.aclose()
        await database.dispose()
    print(f"Embedded {count} entries with {embedder.spec.name}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m standingproof.library")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "reembed", help="embed entries that have no vector from the active model"
    )
    parser.parse_args()
    sys.exit(asyncio.run(_reembed()))


if __name__ == "__main__":
    main()
