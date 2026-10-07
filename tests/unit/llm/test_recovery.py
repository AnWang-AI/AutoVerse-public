"""json_recovery middleware + extraction heuristics (§11, KDR-7)."""

from __future__ import annotations

from autoverse.infra.llm.recovery_mw import (
    RecoveryMiddleware,
    find_and_extract_tool_uses,
)
from autoverse.types import (
    EventType,
    TextDeltaPayload,
    make_event,
)


def test_extract_simple_json_tool_use() -> None:
    text = 'before {"type":"tool_use","id":"t1","name":"f","input":{"a":1}} after'
    _, recovered = find_and_extract_tool_uses(text, set())
    assert recovered == [{"id": "t1", "name": "f", "input": {"a": 1}}]


def test_extract_single_quote_python_dict() -> None:
    text = "{'type': 'tool_use', 'id': 't2', 'name': 'g', 'input': {'x': 2}}"
    _, recovered = find_and_extract_tool_uses(text, set())
    assert recovered == [{"id": "t2", "name": "g", "input": {"x": 2}}]


def test_echo_dedup_skips_executed() -> None:
    text = '{"type":"tool_use","id":"dup","name":"f","input":{}}'
    _, recovered = find_and_extract_tool_uses(text, {"dup"})
    assert recovered == []


def test_non_tool_use_objects_ignored() -> None:
    text = '{"type":"text","value":"hi"} {"type":"tool_use","id":"t","name":"f","input":{}}'
    _, recovered = find_and_extract_tool_uses(text, set())
    assert len(recovered) == 1
    assert recovered[0]["id"] == "t"


def test_middleware_observe_and_flush() -> None:
    mw = RecoveryMiddleware(set())
    parts = [
        '{"type":"tool_use",',
        '"id":"t9","name":"do","input":{"k":"v"}}',
    ]
    for p in parts:
        mw.observe(make_event(TextDeltaPayload(text=p)))
    events = mw.flush()
    assert len(events) == 1
    assert events[0].type is EventType.TOOL_START
    assert events[0].payload.tool_use_id == "t9"
    assert events[0].payload.tool_input == {"k": "v"}


def test_middleware_flush_marks_executed() -> None:
    executed: set[str] = set()
    mw = RecoveryMiddleware(executed)
    mw.observe(
        make_event(
            TextDeltaPayload(
                text='{"type":"tool_use","id":"once","name":"f","input":{}}'
            )
        )
    )
    first = mw.flush()
    assert len(first) == 1
    assert "once" in executed


def test_middleware_flush_does_not_leak_text_into_next_turn() -> None:
    mw = RecoveryMiddleware(set())
    mw.observe(
        make_event(
            TextDeltaPayload(
                text='{"type":"tool_use","id":"t1","name":"first","input":{}}'
            )
        )
    )
    assert len(mw.flush()) == 1
    assert mw.flush() == []
