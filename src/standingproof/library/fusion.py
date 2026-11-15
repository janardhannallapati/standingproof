"""Merge ranked lists from different searches into one ranking.

Keyword scores and vector distances are on unrelated scales, so they cannot be added or
compared. Positions can: being first in a list means the same thing in both.
"""

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Fused[K: Hashable]:
    key: K
    score: float
    found_by: tuple[str, ...]  # names of the lists this item appeared in


def reciprocal_rank_fusion[K: Hashable](
    rankings: Mapping[str, Sequence[K]], *, k: int = 60
) -> list[Fused[K]]:
    """Score each item 1 / (k + rank) in every list it appears in, and sum.

    k damps the difference between neighbouring ranks; 60 is the value from the paper that
    introduced the method and needs no tuning. Ties keep the order items were first seen.
    """
    scores: dict[K, float] = {}
    found_by: dict[K, list[str]] = {}
    for name, ranking in rankings.items():
        unique = list(dict.fromkeys(ranking))  # a repeat counts once, at its best rank
        for rank, key in enumerate(unique, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            found_by.setdefault(key, []).append(name)
    # sorted() is stable and dicts keep insertion order, so equal scores stay in first-seen order.
    ordered = sorted(scores, key=lambda key: -scores[key])
    return [Fused(key, scores[key], tuple(found_by[key])) for key in ordered]
