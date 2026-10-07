"""Unit tests for DeltaThrottle (docs/reference/extensions.md)."""

from __future__ import annotations

from autoverse.frontends.gateway.throttle import DeltaThrottle


def test_merge_all_buffers_until_final() -> None:
    # Non-streaming platform: every delta accumulates, nothing released early.
    clock = [0.0]
    t = DeltaThrottle(merge_all=True, now=lambda: clock[0])
    assert t.add("a") is None
    clock[0] = 100.0  # large time jump still must not flush for merge_all
    assert t.add("b") is None
    assert t.add("c") is None
    assert t.flush_final() == "abc"
    assert t.flush_final() is None  # buffer drained


def test_char_window_flush() -> None:
    clock = [0.0]
    t = DeltaThrottle(char_window=5, time_window_s=999.0, now=lambda: clock[0])
    assert t.add("abc") is None  # 3 chars < 5
    out = t.add("de")  # reaches 5 chars -> flush
    assert out == "abcde"
    assert t.pending() == ""


def test_time_window_flush() -> None:
    clock = [0.0]
    t = DeltaThrottle(char_window=999, time_window_s=1.0, now=lambda: clock[0])
    assert t.add("hi") is None
    clock[0] = 1.5  # cross the time window
    assert t.add("!") == "hi!"


def test_empty_delta_ignored() -> None:
    t = DeltaThrottle(merge_all=True)
    assert t.add("") is None
    assert t.flush_final() is None
