import pytest
from pydantic import BaseModel

from standingproof.agent.tools import Tool, ToolOutcome, ToolRegistry


class NoInput(BaseModel):
    pass


async def _noop(_: NoInput) -> ToolOutcome:
    return ToolOutcome(content="done")


def test_registry_refuses_a_tool_that_can_write() -> None:
    writer = Tool(
        name="send_email",
        description="Sends an email.",
        input_model=NoInput,
        handler=_noop,
        read_only=False,
    )

    with pytest.raises(ValueError, match="read-only"):
        ToolRegistry([writer])
