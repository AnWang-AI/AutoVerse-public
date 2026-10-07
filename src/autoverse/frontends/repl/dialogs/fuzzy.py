"""Dependency-free fuzzy subsequence matcher (docs/reference/commands.md §8, I14).

MiMo uses the ``fuzzysort`` JS library; AutoVerse has no such dependency, so we
implement a small subsequence scorer that is good enough for command / model
palettes (dozens to hundreds of items). Scoring favours:

* contiguous runs of matched characters,
* matches at word boundaries / start of the candidate,
* shorter candidates (tie-break).

``fuzzy_score`` returns ``None`` when *query* is not a subsequence of
*candidate* (case-insensitive); otherwise a float where higher is better.
"""

from __future__ import annotations

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

_BOUNDARY = set(" -_/.:")


def fuzzy_score(query: str, candidate: str) -> float | None:
    """Score *candidate* against *query*; ``None`` if no subsequence match."""
    if not query:
        return 0.0
    q = query.lower()
    c = candidate.lower()
    score = 0.0
    qi = 0
    prev_match = -2
    for ci, ch in enumerate(c):
        if qi >= len(q):
            break
        if ch != q[qi]:
            continue
        # Base point for the match.
        score += 1.0
        # Contiguity bonus: adjacent to the previous matched char.
        if ci == prev_match + 1:
            score += 2.0
        # Boundary bonus: start of string or after a separator.
        if ci == 0 or c[ci - 1] in _BOUNDARY:
            score += 1.5
        prev_match = ci
        qi += 1
    if qi < len(q):
        return None  # not all query chars consumed → not a match
    # Prefer shorter candidates (small, never dominates match bonuses).
    score -= len(c) * 0.01
    return score


def fuzzy_filter(
    query: str,
    items: Iterable[T],
    *,
    key: Callable[[T], str],
) -> list[T]:
    """Return *items* whose ``key`` fuzzily matches *query*, best score first.

    Stable for equal scores (preserves input order via the enumerate index).
    Empty *query* returns all items in their original order.
    """
    if not query.strip():
        return list(items)
    scored: list[tuple[float, int, T]] = []
    for index, item in enumerate(items):
        s = fuzzy_score(query, key(item))
        if s is not None:
            scored.append((s, index, item))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [item for _s, _i, item in scored]


__all__ = ["fuzzy_score", "fuzzy_filter"]
