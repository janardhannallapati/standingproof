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
    UNSUPPORTED_CLAIM = "unsupported_claim"
    MODEL_DECLINED = "model_declined"
    OUTPUT_TRUNCATED = "output_truncated"
    UNEXPECTED_STOP = "unexpected_stop"
    MODEL_ERROR = "model_error"
    # Our own rules, not the model's doing: the day's spend ceiling, and the stop switch.
    DAILY_LIMIT = "daily_limit"
    STOPPED = "stopped"


class AgentResult(BaseModel):
    """The outcome of one run. Exactly one of `draft` and `handoff` is set.

    A draft here has passed the automatic checks. It is a proposal for a human reviewer,
    never something to send to a customer as it stands.
    """

    draft: AnswerDraft | None
    handoff: HandoffReason | None
    steps: int
    cost_usd: float
    usage: Usage
    # Set when a second model read the draft. Its cost is already in `cost_usd`.
    check: "CheckResult | None" = None


class Finding(BaseModel):
    """One thing the checker disputes, as the checker returns it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["unsupported", "contradicted", "not_answered"]
    # The exact words of the draft the finding is about. Empty for `not_answered`.
    quote: str
    # The source the finding rests on. Empty when there is none.
    source_id: str
    explanation: str


class CheckVerdict(BaseModel):
    """The structured reply the checker must return."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["supported", "not_supported"]
    findings: list[Finding]


class CheckTurn(BaseModel):
    """One checker response, reduced to what the code that judges it needs."""

    stop_reason: str
    verdict: CheckVerdict | None
    usage: Usage
    model: str


class CheckedFinding(BaseModel):
    """A finding after the code has looked at it. This is what is stored and shown."""

    kind: Literal["unsupported", "contradicted", "not_answered"]
    quote: str
    # True when `quote` is in the draft, character for character, so it can be marked there.
    located: bool
    source_id: str | None
    explanation: str


class CheckResult(BaseModel):
    """What a check of one wording came to. Decided by `judge_check`, never by the model."""

    outcome: Literal["agrees", "disagrees", "not_checked"]
    findings: list[CheckedFinding]
    # The wording that was read. A check says nothing about any other wording.
    answer_checked: str
    model: str | None
    cost_usd: float
