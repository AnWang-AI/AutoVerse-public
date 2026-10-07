"""Codec base — protocol layer (docs/reference/models.md §3.3, §5.2).

Codecs are pure, network-free, independently unit-testable. They MUST NOT import
transport/httpx (P6 codec/transport split). ``DecodeState`` is the per-stream
accumulator threaded through ``decode_chunk``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from autoverse.types import (
    ContentBlock,
    Message,
    ProviderCapabilities,
    StreamEvent,
    ToolSchema,
    WireKind,
)


@dataclass
class _ToolSlot:
    index: int
    id: str = ""
    name: str = ""
    args_parts: list[str] = field(default_factory=list)
    emitted: bool = False


@dataclass
class DecodeState:
    tools: dict[int, _ToolSlot] = field(default_factory=dict)
    finish_reason: str | None = None
    turn_ended: bool = False

    def tool(self, index: int) -> _ToolSlot:
        slot = self.tools.get(index)
        if slot is None:
            slot = _ToolSlot(index=index)
            self.tools[index] = slot
        return slot

    def finished_tools(self) -> list[_ToolSlot]:
        """Slots not yet emitted, in index order. Marks them emitted so a
        repeated finish chunk (some OpenRouter providers send two
        ``finish_reason==tool_calls`` chunks) does not re-emit TOOL_START."""
        out: list[_ToolSlot] = []
        for i in sorted(self.tools):
            slot = self.tools[i]
            if slot.emitted:
                continue
            slot.emitted = True
            out.append(slot)
        return out


class Codec(Protocol):
    wire: WireKind

    def encode_messages(
        self, messages: list[Message]
    ) -> list[dict[str, object]]: ...

    def encode_tools(
        self, tools: list[ToolSchema]
    ) -> list[dict[str, object]]: ...

    def encode_system(self, system: str, *, cache: bool) -> object: ...

    def decode_chunk(
        self, chunk: dict[str, object], st: DecodeState
    ) -> list[StreamEvent]: ...

    def supports(
        self, blocks: Iterable[ContentBlock], caps: ProviderCapabilities
    ) -> None: ...


# stop_reason normalisation across both wire dialects (§5.2).
_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "END_TURN",
    "stop": "END_TURN",
    "tool_use": "TOOL_USE",
    "tool_calls": "TOOL_USE",
    "max_tokens": "MAX_TOKENS",
    "length": "MAX_TOKENS",
    "stop_sequence": "STOP_SEQUENCE",
}


def normalize_stop_reason(raw: str | None) -> str:
    if raw is None:
        return "END_TURN"
    return _STOP_REASON_MAP.get(raw, "END_TURN")


# JSON-Schema keywords some providers reject at the tool-parameter top level
# (docs/reference/models.md §9.1). Pydantic emits ``$defs``/``$ref`` for nested models
# and combinators (``oneOf`` etc.); strip them from the *top level* so the schema
# the backend sees is the flat object-shape it expects.
_UNSUPPORTED_TOP_LEVEL = ("oneOf", "allOf", "anyOf", "not", "$ref", "$defs", "definitions")


def sanitize_top_level(schema: dict[str, object]) -> dict[str, object]:
    """Return a shallow copy of ``schema`` with unsupported top-level keys dropped."""
    if not isinstance(schema, dict):
        return schema
    return {k: v for k, v in schema.items() if k not in _UNSUPPORTED_TOP_LEVEL}


__all__ = [
    "_ToolSlot",
    "DecodeState",
    "Codec",
    "_STOP_REASON_MAP",
    "normalize_stop_reason",
    "sanitize_top_level",
]
