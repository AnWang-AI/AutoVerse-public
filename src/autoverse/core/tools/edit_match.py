"""Edit matching ladder (docs/reference/tools.md §8.2, T6). Pure functions, no I/O.

FileEditTool tries progressively looser matches to locate `old_string` in the
file content. Each strategy returns (index | None, strategy_name). The
disproportionate guard rejects an edit whose replacement is catastrophically
larger than the matched region (a single char/newline swapped for a whole file).
Its thresholds are deliberately loose (floor=2000 chars, ratio=100x) so ordinary
"small anchor + a block of new code" inserts are never blocked.
"""

from __future__ import annotations

import re

_SMART_QUOTES = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
}


def _normalize_quotes(s: str) -> str:
    for fancy, plain in _SMART_QUOTES.items():
        s = s.replace(fancy, plain)
    return s


def exact_match(content: str, old: str) -> tuple[int | None, str]:
    """Verbatim substring search."""
    idx = content.find(old)
    return (idx if idx >= 0 else None, "exact")


def normalize_quotes_match(content: str, old: str) -> tuple[int | None, str]:
    """Match after normalising smart quotes/dashes on both sides."""
    c = _normalize_quotes(content)
    o = _normalize_quotes(old)
    idx = c.find(o)
    return (idx if idx >= 0 else None, "normalize_quotes")


def normalize_whitespace_match(content: str, old: str) -> tuple[int | None, str]:
    """Match ignoring runs of whitespace; returns index into original content."""
    ws = re.compile(r"\s+")
    o_collapsed = ws.sub(" ", old.strip())
    if not o_collapsed:
        return (None, "normalize_whitespace")
    # Build a regex from the collapsed old-string allowing arbitrary whitespace.
    parts = [re.escape(tok) for tok in o_collapsed.split(" ")]
    pattern = re.compile(r"\s+".join(parts))
    m = pattern.search(content)
    return (m.start() if m else None, "normalize_whitespace")


def disproportionate_guard(
    old: str, new: str, *, ratio: float = 100.0, floor: int = 2000
) -> bool:
    """Return True if the replacement is disproportionately large (reject it).

    Only triggers once `new` is past `floor`, so any replacement up to ``floor``
    characters is always allowed. Past the floor it must also exceed
    ``old_len * ratio`` to be rejected. The thresholds are deliberately loose so
    only catastrophic edits (e.g. a single char/newline replaced by a whole file)
    are caught; ordinary "small anchor + a block of new code" inserts pass.
    """
    old_len = len(old)
    new_len = len(new)
    if new_len <= floor:
        return False
    if old_len == 0:
        return new_len > floor
    return new_len > old_len * ratio


def _count_matches(content: str, old: str, strategy_name: str) -> int:
    """Count non-overlapping matches for the strategy that produced a hit.

    Uniqueness must be checked at the same strategy level that matched
    (docs/reference/tools.md §8.2): if that level matches multiple places, the
    edit is ambiguous unless the caller asked to replace all.
    """
    if strategy_name == "exact":
        return content.count(old) if old else 0
    if strategy_name == "normalize_quotes":
        return _normalize_quotes(content).count(_normalize_quotes(old)) if old else 0
    if strategy_name == "normalize_whitespace":
        ws = re.compile(r"\s+")
        o_collapsed = ws.sub(" ", old.strip())
        if not o_collapsed:
            return 0
        parts = [re.escape(tok) for tok in o_collapsed.split(" ")]
        pattern = re.compile(r"\s+".join(parts))
        return len(pattern.findall(content))
    return 0


def find_match(content: str, old: str) -> tuple[int | None, str]:
    """Run the ladder in order, returning the first hit."""
    for strategy in (exact_match, normalize_quotes_match, normalize_whitespace_match):
        idx, name = strategy(content, old)
        if idx is not None:
            return (idx, name)
    return (None, "none")


def find_match_unique(content: str, old: str) -> tuple[int | None, str, int]:
    """Like `find_match` but also report the match count at the matched level.

    Returns (index, strategy_name, count). `count` is the number of
    non-overlapping matches the winning strategy found, so callers can reject
    ambiguous edits (count > 1) per docs/reference/tools.md §8.2(b).
    """
    idx, name = find_match(content, old)
    if idx is None:
        return (None, "none", 0)
    return (idx, name, _count_matches(content, old, name))


__all__ = [
    "exact_match",
    "normalize_quotes_match",
    "normalize_whitespace_match",
    "disproportionate_guard",
    "find_match",
    "find_match_unique",
]
