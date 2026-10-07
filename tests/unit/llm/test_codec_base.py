"""Codec base helpers: DecodeState slots + stop-reason normalisation."""

from __future__ import annotations

from autoverse.infra.llm.codec.base import (
    DecodeState,
    normalize_stop_reason,
)


def test_decode_state_tool_slot_reuse() -> None:
    st = DecodeState()
    slot = st.tool(0)
    slot.args_parts.append("a")
    assert st.tool(0) is slot
    st.tool(0).args_parts.append("b")
    assert "".join(st.tool(0).args_parts) == "ab"


def test_finished_tools_sorted_by_index() -> None:
    st = DecodeState()
    st.tool(2)
    st.tool(0)
    st.tool(1)
    assert [s.index for s in st.finished_tools()] == [0, 1, 2]


def test_normalize_stop_reason() -> None:
    assert normalize_stop_reason("end_turn") == "END_TURN"
    assert normalize_stop_reason("stop") == "END_TURN"
    assert normalize_stop_reason("tool_use") == "TOOL_USE"
    assert normalize_stop_reason("tool_calls") == "TOOL_USE"
    assert normalize_stop_reason("max_tokens") == "MAX_TOKENS"
    assert normalize_stop_reason("length") == "MAX_TOKENS"
    assert normalize_stop_reason("stop_sequence") == "STOP_SEQUENCE"
    assert normalize_stop_reason(None) == "END_TURN"
    assert normalize_stop_reason("weird") == "END_TURN"
