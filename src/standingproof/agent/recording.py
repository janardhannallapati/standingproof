"""Record real model turns once, replay them in tests forever, at no cost."""

import json
from pathlib import Path
from typing import Any

from standingproof.agent.checker import CheckerClient
from standingproof.agent.loop import ModelClient
from standingproof.agent.steps import plain_content
from standingproof.agent.types import CheckTurn, ModelTurn


class RecordingModel:
    """Wraps a real model and writes every turn to a file."""

    def __init__(self, inner: ModelClient, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._turns: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_addendum: str | None = None,
    ) -> ModelTurn:
        turn = await self._inner.complete(
            system=system, messages=messages, tools=tools, system_addendum=system_addendum
        )
        record = turn.model_dump(mode="json", exclude={"assistant_content"})
        record["assistant_content"] = plain_content(turn.assistant_content)
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
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_addendum: str | None = None,
    ) -> ModelTurn:
        if self.calls >= len(self._turns):
            raise AssertionError(
                "the loop asked for more turns than were recorded; re-record this cassette"
            )
        turn = self._turns[self.calls]
        self.calls += 1
        return turn


class RecordingChecker:
    """Wraps a real checker and writes every reply to a file."""

    def __init__(self, inner: CheckerClient, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._turns: list[dict[str, Any]] = []

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn:
        turn = await self._inner.check(system=system, messages=messages)
        self._turns.append(turn.model_dump(mode="json"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"turns": self._turns}, indent=2) + "\n")
        return turn


class ReplayChecker:
    """Returns recorded checker replies in order. Never touches the network."""

    def __init__(self, path: Path) -> None:
        self._turns = [CheckTurn.model_validate(t) for t in json.loads(path.read_text())["turns"]]
        self.calls = 0

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn:
        if self.calls >= len(self._turns):
            raise AssertionError(
                "the checker was asked more times than were recorded; re-record this cassette"
            )
        turn = self._turns[self.calls]
        self.calls += 1
        return turn
