"""Unit: MessageQueue priority/drain and InputModeStack push/pop semantics."""

from __future__ import annotations

import pytest

from autoverse.frontends.repl.input_queue import (
    InputMode,
    InputModeStack,
    MessageQueue,
)


def test_fifo_drain() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("a")
    q.enqueue("b")
    assert q.dequeue() == "a"
    assert q.dequeue() == "b"
    assert q.dequeue() is None


def test_priority_now_before_next() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("later", priority="next")
    q.enqueue("urgent", priority="now")
    assert q.dequeue() == "urgent"
    assert q.dequeue() == "later"


def test_len_and_bool() -> None:
    q: MessageQueue = MessageQueue()
    assert not q
    q.enqueue("x")
    assert q
    assert len(q) == 1


def test_clear() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("x")
    q.enqueue("y")
    assert q.clear() == 2
    assert len(q) == 0


def test_items_snapshot_order_and_non_mutating() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("n1", priority="next")
    q.enqueue("u1", priority="now")
    q.enqueue("n2", priority="next")
    q.enqueue("l1", priority="later")
    q.enqueue("u2", priority="now")
    # Order mirrors dequeue: now (FIFO) > next (FIFO) > later (FIFO).
    assert q.items() == [
        ("now", "u1"),
        ("now", "u2"),
        ("next", "n1"),
        ("next", "n2"),
        ("later", "l1"),
    ]
    # Snapshot does not consume the queue.
    assert len(q) == 5
    assert q.dequeue() == "u1"


def test_pop_index() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("u1", priority="now")
    q.enqueue("n1", priority="next")
    q.enqueue("n2", priority="next")
    # Flat order is [u1, n1, n2]; pop the middle.
    assert q.pop_index(1) == "n1"
    assert q.items() == [("now", "u1"), ("next", "n2")]
    assert len(q) == 2
    # Out-of-range and negative indices are no-ops returning None.
    assert q.pop_index(5) is None
    assert q.pop_index(-1) is None
    assert len(q) == 2
    # Pop the new first / last.
    assert q.pop_index(0) == "u1"
    assert q.pop_index(0) == "n2"
    assert q.pop_index(0) is None


def test_pop_last() -> None:
    q: MessageQueue = MessageQueue()
    assert q.pop_last() is None  # empty queue → None
    q.enqueue("u1", priority="now")
    q.enqueue("n1", priority="next")
    q.enqueue("n2", priority="next")
    q.enqueue("l1", priority="later")
    # Flat items() order is [u1, n1, n2, l1]; pop_last yields the rightmost.
    assert q.pop_last() == "l1"
    assert q.pop_last() == "n2"
    assert q.items() == [("now", "u1"), ("next", "n1")]
    # With "later" empty, the last flat item is the rightmost "next".
    assert q.pop_last() == "n1"
    # Finally the lone "now" item.
    assert q.pop_last() == "u1"
    assert q.pop_last() is None
    assert len(q) == 0


def test_remove_first() -> None:
    q: MessageQueue = MessageQueue()
    q.enqueue("dup", priority="next")
    q.enqueue("other", priority="next")
    q.enqueue("dup", priority="later")
    # Removes only the first matching item in items() order.
    assert q.remove_first("dup") is True
    assert q.items() == [("next", "other"), ("later", "dup")]
    # No match → False, queue unchanged.
    assert q.remove_first("missing") is False
    assert len(q) == 2


def test_mode_stack_nesting() -> None:
    s = InputModeStack()
    assert s.current() is InputMode.NORMAL
    with s.push(InputMode.RUNNING):
        assert s.current() is InputMode.RUNNING
        with s.push(InputMode.LEADER):
            assert s.current() is InputMode.LEADER
        assert s.current() is InputMode.RUNNING
    assert s.current() is InputMode.NORMAL


def test_mode_stack_pops_on_exception() -> None:
    s = InputModeStack()
    with pytest.raises(ValueError):
        with s.push(InputMode.RUNNING):
            raise ValueError("x")
    assert s.current() is InputMode.NORMAL
