"""Reduce a system's reply to the fields a check needs before it is stored as evidence.

An allow-list, not a block-list: a reply can carry account numbers, e-mail addresses or
URLs we never looked at, and a field nobody listed is a field nobody reviewed.
"""

from typing import Any


def keep(reply: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Copy only the dotted `paths` out of `reply`. Only plain values, never a subtree."""
    kept: dict[str, Any] = {}
    for path in paths:
        source: Any = reply
        parts = path.split(".")
        for part in parts:
            if not isinstance(source, dict) or part not in source:
                source = _MISSING
                break
            source = source[part]
        if source is _MISSING or isinstance(source, dict | list):
            continue
        target = kept
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = source
    return kept


_MISSING = object()
