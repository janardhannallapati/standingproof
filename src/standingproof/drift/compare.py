"""Which named facts differ between two observations of one setting. Pure."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Change:
    fact: str
    # None when the fact was not in that observation.
    was: Any
    now: Any


def _same(was: Any, now: Any) -> bool:
    # Facts are stored as JSON. There `true` and `1` are different values, and in Python
    # `True == 1`, so a boolean is only ever equal to a boolean.
    if isinstance(was, bool) or isinstance(now, bool):
        return isinstance(was, bool) and isinstance(now, bool) and was == now
    return bool(was == now)


def changed_facts(was: dict[str, Any], now: dict[str, Any]) -> list[Change]:
    """The facts whose value differs, by name. Empty means the setting reads the same."""
    return [
        Change(fact, was.get(fact), now.get(fact))
        for fact in sorted(was.keys() | now.keys())
        if not _same(was.get(fact), now.get(fact))
    ]
