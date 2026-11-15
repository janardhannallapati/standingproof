"""A real worker process with a scripted model, for the test that kills one.

    python -m tests.workflow.fake_worker <calls-file>

Everything except the model and the embedder is the production code path: settings from
the environment, Postgres checkpoints, the Redis queue, signal handling. Each model call
is appended to <calls-file>, so the test can count them across processes.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from standingproof.agent.types import AnswerDraft, ModelTurn
from standingproof.config import Settings
from standingproof.workflow.serve import serve
from tests.agent.fakes import finishes, search, wants_tools
from tests.fakes import FakeEmbedder

NO_SOURCE = AnswerDraft(status="no_approved_source", answer="", citations=[], note="Nothing.")


class SlowTwoStepModel:
    """First a search, then "no approved source". Each call takes a moment."""

    def __init__(self, calls_file: Path) -> None:
        self._calls_file = calls_file

    async def complete(self, *, messages: list[dict[str, Any]], **_: Any) -> ModelTurn:
        await asyncio.sleep(0.4)
        first_call = len(messages) == 1
        with self._calls_file.open("a") as log:
            log.write(f"{messages[0]['content']!r} step={1 if first_call else 2}\n")
        if first_call:
            return wants_tools(search("c1", "anything"))
        return finishes(NO_SOURCE)


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(
        serve(
            Settings(_env_file=None),
            SlowTwoStepModel(Path(sys.argv[1])),
            embedder=FakeEmbedder(),
        )
    )
