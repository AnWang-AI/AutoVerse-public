"""gap6 — JSON-Schema top-level sanitizer (R41) + Anthropic tools cache_control (R54)."""

from __future__ import annotations

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.base import sanitize_top_level
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.types import ToolSchema


def test_sanitize_strips_unsupported_top_level_keys() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "$defs": {"Y": {"type": "object"}},
        "$ref": "#/$defs/Y",
        "oneOf": [{"type": "string"}],
        "allOf": [],
        "anyOf": [],
    }
    out = sanitize_top_level(schema)
    assert "type" in out and "properties" in out
    for bad in ("$defs", "$ref", "oneOf", "allOf", "anyOf"):
        assert bad not in out


def test_openai_encode_tools_sanitizes() -> None:
    codec = OpenAIWireCodec()
    t = ToolSchema(name="t", description="d", json_schema={"type": "object", "$defs": {}})
    out = codec.encode_tools([t])
    assert "$defs" not in out[0]["function"]["parameters"]


def test_anthropic_encode_tools_sanitizes() -> None:
    codec = AnthropicWireCodec()
    t = ToolSchema(name="t", description="d", json_schema={"type": "object", "$ref": "#/x"})
    out = codec.encode_tools([t])
    assert "$ref" not in out[0]["input_schema"]
