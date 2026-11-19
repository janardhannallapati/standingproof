"""Hand-written readings for the eval's own tests: what a reader could have replied."""

from evals.cases import Sentence
from evals.reader import Reading, ReadTurn, Said
from evals.transcript import ReaderRun
from standingproof.agent.types import Usage

OFF, TWO = "g-reviews-switched-off", "g-scanning-off-reviews-same"
BP, SS, APP = "github.branch_protection", "github.secret_scanning", "harborlight/app"


def turn(*said: tuple[str, str, str, str], stop_reason: str = "end_turn") -> ReadTurn:
    facts = [
        Said(check=check, scope=APP, fact=fact, says=says, value=value)  # type: ignore[arg-type]
        for check, fact, says, value in said
    ]
    return ReadTurn(
        stop_reason=stop_reason,
        reading=Reading(facts=facts) if stop_reason == "end_turn" else None,
        usage=Usage(input_tokens=800, output_tokens=100),
        model="claude-sonnet-5-5",
    )


_ADMINS = (BP, "admins", "not_stated", "")
NOW_OFF = turn((BP, "protected", "value", "false"), (BP, "reviews", "value", "0"), _ADMINS)
STILL_TWO = turn((BP, "protected", "not_stated", ""), (BP, "reviews", "value", "2"), _ADMINS)
CUT = turn(stop_reason="max_tokens")


def sentence(
    label: str = "states_now",
    part: str = "held_out",
    name: str = "s-1",
    answer: str = "Main is unprotected.",
) -> Sentence:
    return Sentence(name=name, part=part, case=OFF, answer=answer, label=label)  # type: ignore[arg-type]


def read(s: Sentence, reply: ReadTurn | None, run: int = 1) -> ReaderRun:
    return ReaderRun(
        case=s.name,
        run=run,
        variant="main",
        fingerprint={},
        turn=reply,
        cost_usd=0.003,
        read_model="claude-sonnet-5-5",
    )


class ReadsPlainly:
    """A scripted reader for the sentence a check itself writes about branch protection.

    It stands in for the model: it says "not protected" is false and "protected: ... N
    approving reviews" is true with that number, for whichever facts it is asked about.
    """

    def __init__(self, reply: ReadTurn | Exception | None = None) -> None:
        self.reply = reply
        self.requests: list[dict[str, object]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def read(self, *, system: str, messages: list[dict[str, object]]) -> ReadTurn:
        self.requests.append({"system": system, "messages": messages})
        if isinstance(self.reply, Exception):
            raise self.reply
        if self.reply is not None:
            return self.reply
        content = str(messages[0]["content"])
        answer = content.split("<draft>\n")[1].split("\n</draft>")[0]
        unprotected = "not protected" in answer
        number = "0" if unprotected else answer.split(" approving review")[0].split()[-1]
        values = {
            "protected": "false" if unprotected else "true",
            "reviews": number,
            "admins": "false" if unprotected else "true",
        }
        asked = [line.split(" | ") for line in content.split("<facts>\n")[1].splitlines()[:-1]]
        return turn(*[(check, fact, "value", values[fact]) for check, _scope, fact, _kind in asked])
