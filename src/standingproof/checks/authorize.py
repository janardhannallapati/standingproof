"""The one decision in front of every check: may this tenant run this check on this scope?

Default deny. A check runs only if it is registered, the scope has the right shape, and the
tenant has connected that scope. Pure: it is given the tenant's connections and returns a
decision, so every rule is tested without a database or a network.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from standingproof.checks.registry import CheckDef

# A scope becomes part of a URL path or an API argument, so its shape is checked first.
# GitHub names: letters, digits, hyphen, underscore, dot. No slash, no "..", no query.
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9_-]|\.(?!\.))*"
_SCOPE_SHAPES = {
    "org": re.compile(_NAME),
    "repo": re.compile(f"{_NAME}/{_NAME}"),
    "account": re.compile(r"[0-9]{12}"),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Why not: "unknown_check", "invalid_scope" or "scope_not_connected". None when allowed.
    reason: str | None = None


def valid_scope(kind: str, scope: str) -> bool:
    return _SCOPE_SHAPES[kind].fullmatch(scope) is not None


def authorize(
    check: CheckDef | None, scope: str, connections: Iterable[tuple[str, str]]
) -> Decision:
    """`connections` is the tenant's connected (system, scope) pairs."""
    if check is None:
        return Decision(False, "unknown_check")
    if not valid_scope(check.scope_kind, scope):
        return Decision(False, "invalid_scope")
    # A repository belongs to the organisation before the slash; that is what is connected.
    owner = scope.split("/")[0].lower()
    connected = {s.lower() for system, s in connections if system == check.system}
    if owner not in connected:
        return Decision(False, "scope_not_connected")
    return Decision(True)
