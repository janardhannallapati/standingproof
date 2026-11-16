"""What the graph remembers about one question, and what a run of it is given.

The state is saved to the database after every step, so it holds plain JSON values only:
no SDK objects, no clients. Everything that cannot be saved (the model client, the tools)
arrives through the context, fresh on every run.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from standingproof.agent.checker import CheckerClient
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelClient
from standingproof.agent.prompts import question_message
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import AnswerDraft, CheckResult, Usage

NO_APPROVED_SOURCE = "no_approved_source"


class QuestionState(TypedDict):
    question: str
    # The conversation so far, exactly as it is sent to the provider.
    messages: list[dict[str, Any]]
    # Tool calls the last model turn asked for, waiting to be run.
    pending_calls: list[dict[str, Any]]
    seen_calls: list[str]
    seen_sources: dict[str, list[str]]
    usage: dict[str, int]
    cost_usd: float
    steps: int
    # Seconds spent inside model calls and tools. Time the worker was down is not counted.
    elapsed: float
    # Set by the step that ends a round of work. None while the question is being worked on.
    outcome: dict[str, Any] | None
    # The model's last finished turn, kept so a redraft can continue the conversation.
    last_turn: list[dict[str, Any]] | None
    # The reviewer's words for the current redraft. Sent in the system message, never in
    # the conversation, where the questionnaire's own text lives.
    instruction: str | None
    # How many times a reviewer has sent the question back.
    redrafts: int
    # Totals when the current round began. The per-question limits apply to one round.
    round_start: dict[str, float]


def initial_state(question: str) -> QuestionState:
    return QuestionState(
        question=question,
        messages=[{"role": "user", "content": question_message(question)}],
        pending_calls=[],
        seen_calls=[],
        seen_sources={},
        usage=Usage().model_dump(),
        cost_usd=0.0,
        steps=0,
        elapsed=0.0,
        outcome=None,
        last_turn=None,
        instruction=None,
        redrafts=0,
        round_start={"steps": 0, "cost_usd": 0.0, "elapsed": 0.0},
    )


@dataclass(frozen=True)
class ExactMatch:
    """An approved answer whose question is the same as the one being asked."""

    source_id: str
    answer: str


@dataclass(frozen=True)
class QuestionContext:
    """What one run needs and the state must not hold."""

    model: ModelClient
    registry: ToolRegistry
    limits: Limits
    find_exact: Callable[[str], Awaitable[ExactMatch | None]]
    clock: Callable[[], float] = time.monotonic
    # The second reader of each draft. None: drafts are not checked.
    checker: CheckerClient | None = None


@dataclass(frozen=True)
class Outcome:
    """How one question ended. A draft here is a proposal for a reviewer, never an answer."""

    status: Literal["ready", "needs_review"]
    # reused: an approved answer matched exactly. drafted: the model wrote it from sources.
    # None when the question went to a person with nothing written.
    path: Literal["reused", "drafted"] | None
    reason: str | None
    draft: AnswerDraft | None
    steps: int
    cost_usd: float
    usage: Usage
    # What the second reader made of the draft. None when the draft was not sent to one.
    check: CheckResult | None = None


@dataclass(frozen=True)
class Progress:
    """What a question has used so far, finished or not."""

    steps: int
    cost_usd: float
    usage: Usage
