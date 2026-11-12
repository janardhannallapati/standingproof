import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from standingproof.agent.types import ToolCall


@dataclass(frozen=True)
class ToolOutcome:
    """What goes back to the model for one tool call."""

    content: str
    is_error: bool = False
    # Identifiers of sources this call returned. An answer may cite only these.
    source_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[ToolOutcome]]
    read_only: bool

    def definition(self) -> dict[str, Any]:
        """The JSON the model sees. `strict` makes the provider enforce the schema."""
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    """The only tools a run may use. Anything not registered here is denied."""

    def __init__(self, tools: list[Tool]) -> None:
        writers = [t.name for t in tools if not t.read_only]
        if writers:
            # Model-facing tools are read-only by design. Writes happen in plain code,
            # after a recorded human approval.
            raise ValueError(f"model-facing tools must be read-only: {writers}")
        self._tools = {t.name: t for t in tools}

    def definitions(self) -> list[dict[str, Any]]:
        # Sorted so the request prefix is byte-identical between calls (prompt caching).
        return [self._tools[name].definition() for name in sorted(self._tools)]

    async def execute(self, call: ToolCall) -> ToolOutcome:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolOutcome(
                content=(
                    f"Error: there is no tool named {call.name!r}. "
                    f"Available tools: {', '.join(sorted(self._tools))}."
                ),
                is_error=True,
            )
        try:
            arguments = tool.input_model.model_validate(call.arguments)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
                for e in error.errors()
            )
            expected = json.dumps(tool.input_model.model_json_schema().get("properties", {}))
            return ToolOutcome(
                content=f"Error: invalid arguments ({problems}). Expected fields: {expected}",
                is_error=True,
            )
        return await tool.handler(arguments)
