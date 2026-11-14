"""Record real model turns once, replay them in tests forever, at no cost."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from standingproof.agent.loop import ModelClient
from standingproof.agent.types import ModelTurn


def _to_json(content: Any) -> Any:
    if isinstance(content, BaseModel):
        return content.model_dump(mode="json", exclude_none=True)
    if isinstance(content, list):
        return [_to_json(item) for item in content]
    return content


class RecordingModel:
    """Wraps a real model and writes every turn to a file."""

    def __init__(self, inner: ModelClient, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._turns: list[dict[str, Any]] = []

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        turn = await self._inner.complete(system=system, messages=messages, tools=tools)
        record = turn.model_dump(mode="json", exclude={"assistant_content"})
        record["assistant_content"] = _to_json(turn.assistant_content)
        self._turns.append(record)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"turns": self._turns}, indent=2) + "\n")
        return turn


class ReplayModel:
    """Returns recorded turns in order. Never touches the network."""

    def __init__(self, path: Path) -> None:
        self._turns = [ModelTurn.model_validate(t) for t in json.loads(path.read_text())["turns"]]
        self.calls = 0

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        if self.calls >= len(self._turns):
            raise AssertionError(
                "the loop asked for more turns than were recorded; re-record this cassette"
            )
        turn = self._turns[self.calls]
        self.calls += 1
        return turn
