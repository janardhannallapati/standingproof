"""Library maintenance commands.

    python -m standingproof.library reembed

Gives every stored entry that lacks one a vector from the embedding model named in the
settings. Run it once after changing SP_EMBEDDING_MODEL.
"""

import argparse
import asyncio
import sys

from standingproof.config import get_settings
from standingproof.db import build_engine, build_session_factory
from standingproof.library.embedding import EmbeddingConfigError, EmbeddingError, build_embedder
from standingproof.library.service import reembed_missing


async def _reembed() -> int:
    settings = get_settings()
    try:
        embedder = build_embedder(settings)
    except EmbeddingConfigError as error:
        print(error, file=sys.stderr)
        return 2
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        count = await reembed_missing(build_session_factory(engine), embedder)
    except EmbeddingError as error:
        print(f"Stopped: {error} Entries already embedded are kept; run again.", file=sys.stderr)
        return 1
    finally:
        await embedder.aclose()
        await engine.dispose()
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
