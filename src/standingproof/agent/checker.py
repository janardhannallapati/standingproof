"""The independent checker: a second model call that reads a draft against its sources.

It starts from nothing. Its request is built here from three things, the question, the
draft's wording and the sources, and from no part of the conversation that produced the
draft. It has no tools: the interface below has nowhere to pass one.
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any, Protocol

from standingproof.agent.prompts import CHECKER_PROMPT, wrap
from standingproof.agent.steps import judge_check
from standingproof.agent.types import AnswerDraft, CheckResult, CheckTurn, Usage


class CheckerClient(Protocol):
    """The one thing the checker needs from a model provider. Note what is missing: tools."""

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn: ...


def check_messages(
    question: str, draft: AnswerDraft, sources: Mapping[str, Sequence[str]]
) -> list[dict[str, Any]]:
    """The checker's whole request. The only place one is built.

    Left out on purpose: the drafter's messages, its thinking, its searches and its note.
    Each of those argues for the draft, and a reader who is shown the argument tends to
    agree with it.
    """
    parts = [wrap("question", question), wrap("draft", draft.answer)]
    for source_id, texts in sources.items():
        cited = "yes" if source_id in draft.citations else "no"
        # The same source can come back from two searches; one copy is enough.
        text = "\n".join(dict.fromkeys(texts))
        parts.append(wrap("source", text, f' id="{escape(source_id)}" cited="{cited}"'))
    return [{"role": "user", "content": "\n\n".join(parts)}]


async def run_check(
    checker: CheckerClient,
    *,
    question: str,
    draft: AnswerDraft,
    sources: Mapping[str, Sequence[str]],
) -> tuple[CheckResult, Usage]:
    """One check, and what it used. Raises what the client raises."""
    turn = await checker.check(
        system=CHECKER_PROMPT, messages=check_messages(question, draft, sources)
    )
    return judge_check(turn, draft.answer, sources.keys()), turn.usage
