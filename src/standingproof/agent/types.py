from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Usage(BaseModel):
    """Token counts for one model call, as the provider bills them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ToolCall(BaseModel):
    """A request from the model to run a tool. Nothing has run yet."""

    id: str
    name: str
    arguments: dict[str, Any]


class AnswerDraft(BaseModel):
    """The structured final answer the model must return."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["answered", "no_approved_source"]
    answer: str
    citations: list[str]
    note: str


class ModelTurn(BaseModel):
    """One model response, reduced to what the loop needs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stop_reason: str
    tool_calls: list[ToolCall]
    draft: AnswerDraft | None
    usage: Usage
    model: str
    # Sent back to the provider unchanged on the next call. Opaque to the loop.
    assistant_content: Any


class HandoffReason(StrEnum):
    """Why the loop stopped without an accepted answer. Each one routes to a person."""

    STEP_LIMIT = "step_limit"
    COST_LIMIT = "cost_limit"
    TIME_LIMIT = "time_limit"
    REPEATED_CALL = "repeated_call"
    UNGROUNDED_ANSWER = "ungrounded_answer"
    MODEL_DECLINED = "model_declined"
    OUTPUT_TRUNCATED = "output_truncated"
    UNEXPECTED_STOP = "unexpected_stop"


class AgentResult(BaseModel):
    """The outcome of one run. Exactly one of `draft` and `handoff` is set."""

    draft: AnswerDraft | None
    handoff: HandoffReason | None
    steps: int
    cost_usd: float
    usage: Usage
