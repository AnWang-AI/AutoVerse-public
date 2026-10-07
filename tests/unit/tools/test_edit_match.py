"""Unit: edit matching ladder."""

from __future__ import annotations

from autoverse.core.tools.edit_match import (
    disproportionate_guard,
    exact_match,
    find_match,
    find_match_unique,
    normalize_quotes_match,
    normalize_whitespace_match,
)


def test_exact_match() -> None:
    idx, name = exact_match("hello world", "world")
    assert idx == 6 and name == "exact"
    assert exact_match("abc", "z")[0] is None


def test_normalize_quotes_match() -> None:
    content = "say \u201chello\u201d now"  # smart quotes
    idx, name = normalize_quotes_match(content, '"hello"')
    assert idx is not None and name == "normalize_quotes"


def test_normalize_whitespace_match() -> None:
    content = "def  foo(  x ):\n    pass"
    idx, name = normalize_whitespace_match(content, "def foo( x ):")
    assert idx is not None and name == "normalize_whitespace"


def test_find_match_ladder_order() -> None:
    assert find_match("plain text here", "text")[1] == "exact"
    assert find_match("a\u2019b", "a'b")[1] == "normalize_quotes"


def test_find_match_unique_counts_multiple() -> None:
    idx, name, count = find_match_unique("foo foo foo", "foo")
    assert idx == 0 and name == "exact" and count == 3


def test_find_match_unique_single() -> None:
    idx, name, count = find_match_unique("alpha beta", "beta")
    assert idx is not None and count == 1


def test_find_match_unique_no_match() -> None:
    idx, name, count = find_match_unique("abc", "zzz")
    assert idx is None and name == "none" and count == 0


def test_disproportionate_guard() -> None:
    # Catastrophic: tiny anchor swapped for a huge blob (past floor AND >100x).
    assert disproportionate_guard("x", "y" * 5000) is True
    assert disproportionate_guard("hello", "hi") is False
    assert disproportionate_guard("a", "abc") is False  # below floor
    # Anything up to the 2000-char floor is always allowed, regardless of ratio.
    assert disproportionate_guard("x", "y" * 2000) is False
    # Past the floor but within 100x of old_string -> still allowed.
    assert disproportionate_guard("y" * 100, "z" * 5000) is False
