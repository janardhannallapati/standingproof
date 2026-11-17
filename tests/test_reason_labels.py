"""Every reason the agent can give a reviewer has words on the screen.

The screen falls back to the code with its underscores removed, which reads badly and was
how `daily_limit` would have appeared. The reasons are read from the enum, so a new one
fails here until it has a label.
"""

import re
from pathlib import Path

from standingproof.agent.types import HandoffReason

LABELS = Path(__file__).parents[1] / "web" / "src" / "lib" / "labels.ts"


def test_every_handoff_reason_has_a_label_in_the_reviewers_screen() -> None:
    table = LABELS.read_text().split("const REASONS", 1)[1].split("};", 1)[0]
    labelled = set(re.findall(r"^\s*([a-z_]+):", table, flags=re.M))

    assert {reason.value for reason in HandoffReason} <= labelled
