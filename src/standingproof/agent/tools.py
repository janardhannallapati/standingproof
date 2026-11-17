import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from standingproof.agent.types import ToolCall

# The start of the id of what a look at a live setting returned. Only `checks/tools.py`
# makes such an id, so it tells an observation from a document that says it is one.
OBSERVED_PREFIX = "evidence:"


@dataclass(frozen=True)
class ToolOutcome:
    """What goes back to the model for one tool call."""

    content: str
    is_error: bool = False
    # Sources this call returned, as id -> text. An answer may cite only these ids, and its
    # wording is checked against these texts.
    sources: Mapping[str, str] = field(default_factory=dict)


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

    def with_tools(self, *tools: Tool) -> "ToolRegistry":
        """A new registry holding these tools as well. The same read-only rule applies."""
        return ToolRegistry([*self._tools.values(), *tools])

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
