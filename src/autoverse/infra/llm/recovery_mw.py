"""json_recovery — optional middleware (§11, KDR-7).

Heuristic recovery for degradation_prone backends that emit tool calls as plain
text. Pure ``find_and_extract_tool_uses`` plus a ``RecoveryMiddleware`` that
buffers TEXT_DELTA and re-emits recovered tool uses as TOOL_START on flush. Only
mounted by the Router for degradation_prone backends (not wired into
``RecoveryPolicy``; recovery policy owns turn-level retry decisions).
"""

from __future__ import annotations

import ast
import json

from autoverse.types import (
    EventType,
    StreamEvent,
    TextDeltaPayload,
    ToolStartPayload,
    make_event,
)


def _try_parse(blob: str) -> dict[str, object] | None:
    candidate = blob.strip()
    if not candidate:
        return None
    # 1) strict JSON
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) lenient: strip trailing commas before } or ]
    cleaned = candidate.replace(",}", "}").replace(",]", "]")
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 3) single-quote python-dict form via ast.literal_eval
    try:
        obj = ast.literal_eval(candidate)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError):
        pass
    return None


def _scan_objects(text: str) -> list[dict[str, object]]:
    """Brace-counting scan: yield each balanced top-level {...} that parses."""
    out: list[dict[str, object]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    parsed = _try_parse(text[start : i + 1])
                    if parsed is not None:
                        out.append(parsed)
                    start = -1
    return out


def find_and_extract_tool_uses(
    text: str, executed: set[str]
) -> tuple[str, list[dict[str, object]]]:
    """Extract tool_use objects embedded as text.

    Returns (remaining_text, recovered) where each recovered dict has keys
    ``id``/``name``/``input`` suitable for ToolUseBlock(**tu). Echoed tool ids
    already in ``executed`` are dropped (no re-execution).
    """
    recovered: list[dict[str, object]] = []
    for obj in _scan_objects(text):
        if obj.get("type") != "tool_use":
            continue
        tool_id = str(obj.get("id", ""))
        name = str(obj.get("name", ""))
        raw_input = obj.get("input", {})
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        if tool_id and tool_id in executed:
            continue
        if name == "":
            continue
        recovered.append({"id": tool_id, "name": name, "input": tool_input})
    return text, recovered


class RecoveryMiddleware:
    def __init__(self, executed_ids: set[str]) -> None:
        self._buf: list[str] = []
        self._executed = executed_ids

    def observe(self, ev: StreamEvent) -> StreamEvent:
        if ev.type is EventType.TEXT_DELTA:
            payload = ev.payload
            assert isinstance(payload, TextDeltaPayload)
            self._buf.append(payload.text)
        return ev

    def reset(self) -> None:
        self._buf.clear()

    def flush(self) -> list[StreamEvent]:
        text = "".join(self._buf)
        self._buf.clear()
        _, recovered = find_and_extract_tool_uses(text, self._executed)
        events: list[StreamEvent] = []
        for tu in recovered:
            tool_id = str(tu["id"])
            name = str(tu["name"])
            tool_input = tu["input"]
            assert isinstance(tool_input, dict)
            if tool_id:
                self._executed.add(tool_id)
            events.append(
                make_event(
                    ToolStartPayload(
                        tool_use_id=tool_id,
                        tool_name=name,
                        tool_input=tool_input,
                    )
                )
            )
        return events


__all__ = ["find_and_extract_tool_uses", "RecoveryMiddleware"]
