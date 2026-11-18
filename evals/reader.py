"""What an answer says a setting reads now, and whether that is what the world reads.

Two cases change a setting between a draft and its redraft. Their right answer cannot be
told from a stale one by a pattern: "two approving reviews" is stale as a statement and
correct inside "the policy asks for two, but main is not protected". So the wording is
read by a model, and the reading is compared here, in code, with the case's world.

The model is asked what the answer says, never whether the answer is right. It is shown
the question, the answer and the names of the facts, and nothing of the case's world, its
sources or what is expected, so it has nothing to agree with.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from evals.cases import DrafterCase

Key = tuple[str, str, str]


class Said(BaseModel):
    """One fact of one setting, as the answer puts it."""

    model_config = ConfigDict(extra="forbid")

    check: str
    scope: str
    fact: str
    # "value": the answer says what the fact is now, once. "conflicting": it says two
    # different things about now. "not_stated": it says nothing about now.
    says: Literal["value", "not_stated", "conflicting"]
    # "true", "false" or a whole number when `says` is "value"; empty otherwise.
    value: str


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[Said]


def _plain(value: object) -> str:
    """A value as it is compared: true, false or a number without leading zeros."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    return str(int(text)) if text.isdecimal() else text


def expected(case: DrafterCase) -> dict[Key, str]:
    """What each fact the case asks about reads in the world the answer is about."""
    reads = {(live.check, live.scope): live.reads or {} for live in case.now}
    return {
        (state.check, state.scope, fact): _plain(reads[state.check, state.scope][fact])
        for state in case.expect.states
        for fact in state.facts
    }


def states_now(case: DrafterCase, reading: Reading) -> list[str]:
    """What is wrong with the answer that was read, as an answer to the case. Empty when
    nothing is: no fact is given another value or two values, and every setting the case
    names has at least one fact stated as the world has it."""
    said: dict[Key, list[Said]] = {}
    for item in reading.facts:
        said.setdefault((item.check, item.scope, item.fact), []).append(item)
    world = expected(case)
    problems: list[str] = []
    for state in case.expect.states:
        stated = False
        for fact in state.facts:
            what = f"{fact} of {state.check}"
            found = said.get((state.check, state.scope, fact), [])
            if len(found) != 1:
                problems.append(f"{'no' if not found else 'more than one'} reading of {what}")
                continue
            if found[0].says == "conflicting":
                problems.append(f"says two things about {what}")
            elif found[0].says == "value":
                now = world[state.check, state.scope, fact]
                if _plain(found[0].value) == now:
                    stated = True
                else:
                    problems.append(f"says {what} reads {found[0].value.strip()}; it reads {now}")
        if not stated:
            problems.append(f"does not state what {state.check} of {state.scope} reads now")
    return problems
