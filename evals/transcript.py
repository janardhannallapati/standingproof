"""What one run of one case left behind. This is what is committed and graded again in CI.

A recording holds what the model returned, before any of our rules looked at it: the final
draft as the drafter gave it and the checker's reply as the checker gave it. Grading applies
today's rules to those, so a change to a rule shows up in the score without a paid run.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from standingproof.agent.types import AnswerDraft, CheckTurn, Usage

RECORDINGS = Path(__file__).parent / "recordings"
MANIFEST = "RUN.json"


class SecretShapedError(ValueError):
    """A recording that would put something shaped like a credential in a public file."""


class MisplacedError(Exception):
    """A file among the recordings that is not a recording in its own place."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolUse(_Record):
    name: str
    arguments: dict[str, Any]
    is_error: bool
    # The sources this call returned. An answer may cite only these.
    source_ids: list[str]


class DrafterRun(_Record):
    kind: Literal["drafter"] = "drafter"
    case: str
    run: int
    # "docs_only": the same case with the live check tool taken away.
    variant: Literal["full", "docs_only"]
    fingerprint: dict[str, str]
    # Why the loop said it stopped, at the time. None when it ended with a draft.
    handoff: str | None
    # The last model turn: why it stopped and the draft it carried, if any.
    final_stop_reason: str | None
    final_draft: AnswerDraft | None
    steps: int
    tool_uses: list[ToolUse]
    seen_sources: dict[str, list[str]]
    # The checker's reply. None when no check was made.
    check_turn: CheckTurn | None
    usage: Usage
    cost_usd: float
    draft_model: str
    check_model: str | None
    # A redraft case (family G): the draft that was sent back, and where in `tool_uses` the
    # redraft's own calls begin. Everything above describes the redraft. None otherwise.
    first_draft: AnswerDraft | None = None
    redraft_from: int | None = None


class CheckerRun(_Record):
    kind: Literal["checker"] = "checker"
    case: str
    run: int
    # "cheaper": the same drafts read by a smaller model, for comparison.
    variant: Literal["main", "cheaper"]
    fingerprint: dict[str, str]
    # None when the provider could not be reached.
    turn: CheckTurn | None
    cost_usd: float
    check_model: str


Run = DrafterRun | CheckerRun
_RUN: TypeAdapter[Run] = TypeAdapter(Run)


def path_of(run: Run, root: Path = RECORDINGS) -> Path:
    return root / f"{run.kind}-{run.variant}" / f"{run.case}.{run.run}.json"


def write(run: Run, root: Path = RECORDINGS) -> Path:
    path = path_of(run, root)
    data = run.model_dump(mode="json")
    text = json.dumps(data, indent=1) + "\n"
    if holds_secret(data):
        # These files are committed to a public repository.
        raise SecretShapedError(f"{path.name} holds text shaped like a credential; not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def read_all(root: Path = RECORDINGS) -> list[Run]:
    """Every recording under `root`, in a stable order.

    A JSON file that is not where its own contents say it belongs is refused: a copy under
    another name would otherwise count as another run, and a file one level off would be
    skipped without a word.
    """
    runs: list[Run] = []
    for path in sorted(root.rglob("*.json")):
        if path == root / MANIFEST:
            continue
        try:
            run = _RUN.validate_json(path.read_bytes())
        except ValueError:
            raise MisplacedError(f"{path.relative_to(root)} is not a recording") from None
        if path_of(run, root) != path:
            raise MisplacedError(f"{path.relative_to(root)} is not where its contents belong")
        runs.append(run)
    return runs


# Shapes some credentials have: provider keys, GitHub tokens, AWS key ids, a password in
# a URL, a private key. Not every secret has a shape (an AWS secret key does not), so this
# is a net under the real control, which is that a recording is built only from the case,
# the fake systems and the model's reply. `write` refuses a match and a test scans what is
# committed.
#
# A key id is bounded by "not another capital or digit" and not by a word boundary: an
# underscore, or the letter that ends an escape, is a word character and would hide it.
_SECRET = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{10,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|(?<![A-Z0-9])A[KS]IA[0-9A-Z]{16}(?![A-Z0-9])"
    r"|(?<![A-Za-z0-9-])pa-[A-Za-z0-9_-]{20,}"
    r"|://[^/\s:@]{0,64}:[^/\s:@]{1,128}@"
    r"|-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
)


def looks_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def holds_secret(value: object) -> bool:
    """Whether any string inside a JSON value is shaped like a credential.

    The strings are looked at as themselves, not as JSON text. In JSON text a line break is
    a backslash and the letter n, and a pattern that looks at what stands before a key
    would see that letter instead of the break.
    """
    if isinstance(value, str):
        return looks_secret(value)
    if isinstance(value, dict):
        return any(holds_secret(item) for pair in value.items() for item in pair)
    if isinstance(value, list):
        return any(holds_secret(item) for item in value)
    return False
