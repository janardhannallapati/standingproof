"""Workflow commands.

    python -m standingproof.workflow setup     create or upgrade the saved-state tables
    python -m standingproof.workflow worker    answer queued questions until stopped

`setup` is safe to run on every deploy, after the database migrations. `worker` calls the
paid model API for every question that has no exact approved answer.
"""

import argparse
import asyncio
import logging
import sys
from typing import Any

import anthropic

from standingproof.agent.loop import ModelCallError
from standingproof.agent.model import AnthropicModel
from standingproof.agent.types import ModelTurn
from standingproof.config import get_settings
from standingproof.workflow.checkpoint import setup
from standingproof.workflow.serve import serve


class NoModel:
    """Stands in when no model key is set: every call is refused, permanently."""

    async def complete(self, **_: Any) -> ModelTurn:
        raise ModelCallError("no model key is configured")


async def _worker() -> int:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        # Still useful: exact approved answers are reused with no model. Every other
        # question is handed to a person with the reason `model_error`.
        logging.getLogger(__name__).warning(
            "ANTHROPIC_API_KEY is not set: questions without an exact approved answer "
            "will go to review undrafted"
        )
        await serve(settings, NoModel())
        return 0
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=60.0, max_retries=2
    )
    model = AnthropicModel(client, model=settings.draft_model, effort=settings.draft_effort)
    try:
        await serve(settings, model)
    finally:
        await client.close()
    return 0


async def _setup() -> int:
    await setup(get_settings().database_url.get_secret_value())
    print("Saved-state tables are ready.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m standingproof.workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="create or upgrade the saved-state tables")
    commands.add_parser("worker", help="answer queued questions until stopped")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(_worker() if args.command == "worker" else _setup()))


if __name__ == "__main__":
    main()
