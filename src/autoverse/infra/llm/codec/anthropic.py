"""Anthropic wire codec (docs/reference/models.md §5.1, §9.1, §10, §12).

Pure, network-free. Encodes internal Messages to the content-block dialect and
decodes SSE chunks back into unified StreamEvents. Tool calls decode to
TOOL_START events carrying the fully-assembled tool_input (TOOL_END is a
core/tools execution artifact, not a codec product).
"""

from __future__ import annotations

import json
from typing import Iterable

from autoverse.infra.llm.codec.base import (
    DecodeState,
    normalize_stop_reason,
)
from autoverse.infra.llm.taxonomy import ErrorKind, LLMError
from autoverse.types import (
    ContentBlock,
    ImageBlock,
    Message,
    ProviderCapabilities,
    StreamEvent,
    TextBlock,
    TextDeltaPayload,
    ThinkingBlock,
    ThinkingDeltaPayload,
    ToolResultBlock,
    ToolSchema,
    ToolStartPayload,
    ToolUseBlock,
    TurnEndPayload,
    Usage,
    UsagePayload,
    WireKind,
    make_event,
)

# Minimum system length (chars) before injecting cache_control on the static
# block (§12 minimal-length gate; ~2048 tokens ≈ 8000 chars heuristic).
_CACHE_MIN_CHARS = 8000
# Dynamic boundary marker: text after this is volatile and never cached (§12).
_DYNAMIC_BOUNDARY = "## Working Directory"


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


class AnthropicWireCodec:
    wire: WireKind = WireKind.ANTHROPIC

    def _block_to_anthropic(self, b: ContentBlock) -> dict[str, object]:
        if isinstance(b, TextBlock):
            return {"type": "text", "text": b.text}
        if isinstance(b, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": b.id,
                "name": b.name,
                "input": b.input,
            }
        if isinstance(b, ToolResultBlock):
            return {
                "type": "tool_result",
                "tool_use_id": b.tool_use_id,
                "content": b.content,
                "is_error": b.is_error,
            }
        if isinstance(b, ImageBlock):
            if b.source_type == "url":
                return {
                    "type": "image",
                    "source": {"type": "url", "url": b.data},
                }
            from autoverse.infra.llm.codec.image import clamp_image_base64

            media_type, data = clamp_image_base64(b.media_type, b.data)
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        if isinstance(b, ThinkingBlock):
            block: dict[str, object] = {"type": "thinking", "thinking": b.text}
            if b.signature is not None:
                block["signature"] = b.signature
            return block
        raise LLMError(
            f"unsupported content block: {type(b).__name__}",
            kind=ErrorKind.UNSUPPORTED,
        )

    def encode_messages(self, messages: list[Message]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for msg in messages:
            role = "user" if msg.role == "system" else msg.role
            # tool results are conveyed as user-role blocks in anthropic wire.
            if any(isinstance(b, ToolResultBlock) for b in msg.content):
                role = "user"
            blocks = [self._block_to_anthropic(b) for b in msg.content]
            if out and out[-1]["role"] == role:
                existing = out[-1]["content"]
                assert isinstance(existing, list)
                existing.extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return out

    def encode_tools(self, tools: list[ToolSchema]) -> list[dict[str, object]]:
        from autoverse.infra.llm.codec.base import sanitize_top_level

        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": sanitize_top_level(t.json_schema),
            }
            for t in tools
        ]

    def encode_system(self, system: str, *, cache: bool) -> object:
        if not cache or len(system) <= _CACHE_MIN_CHARS:
            return system
        idx = system.find(_DYNAMIC_BOUNDARY)
        if idx <= 0:
            static, dynamic = system, ""
        else:
            static, dynamic = system[:idx], system[idx:]
        blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": static,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if dynamic:
            blocks.append({"type": "text", "text": dynamic})
        return blocks

    def decode_chunk(self, chunk: dict[str, object], st: DecodeState) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        ctype = chunk.get("type")
        index = int(_as_int(chunk.get("index"), 0))

        if ctype == "content_block_start":
            block = _as_dict(chunk.get("content_block"))
            if block.get("type") == "tool_use":
                slot = st.tool(index)
                slot.id = str(block.get("id", ""))
                slot.name = str(block.get("name", ""))

        elif ctype == "content_block_delta":
            delta = _as_dict(chunk.get("delta"))
            dtype = delta.get("type")
            if dtype == "text_delta":
                events.append(
                    make_event(TextDeltaPayload(text=str(delta.get("text", ""))))
                )
            elif dtype == "thinking_delta":
                events.append(
                    make_event(
                        ThinkingDeltaPayload(text=str(delta.get("thinking", "")))
                    )
                )
            elif dtype == "input_json_delta":
                st.tool(index).args_parts.append(
                    str(delta.get("partial_json", ""))
                )

        elif ctype == "content_block_stop":
            slot_opt = st.tools.get(index)
            # Idempotent: a repeated content_block_stop for the same index must
            # not re-emit TOOL_START (mirrors the OpenAI codec guard).
            if slot_opt is not None and not slot_opt.emitted:
                slot_opt.emitted = True
                raw = "".join(slot_opt.args_parts) or "{}"
                try:
                    parsed_obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    parsed_obj = {}
                parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
                events.append(
                    make_event(
                        ToolStartPayload(
                            tool_use_id=slot_opt.id,
                            tool_name=slot_opt.name,
                            tool_input=parsed,
                        )
                    )
                )

        elif ctype in ("message_start", "message_delta"):
            usage = self._extract_usage(chunk)
            if usage is not None:
                events.append(make_event(usage))
            delta = _as_dict(chunk.get("delta"))
            if delta.get("stop_reason"):
                st.finish_reason = str(delta["stop_reason"])

        elif ctype == "message_stop" and not st.turn_ended:
            st.turn_ended = True
            raw_stop = chunk.get("stop_reason")
            stop = str(raw_stop) if raw_stop else st.finish_reason
            events.append(
                make_event(
                    TurnEndPayload(
                        turn_index=0,
                        iterations=0,
                        stop_cause=normalize_stop_reason(stop),
                    )
                )
            )
        return events

    def _extract_usage(self, chunk: dict[str, object]) -> UsagePayload | None:
        raw = chunk.get("usage")
        if raw is None:
            msg = _as_dict(chunk.get("message"))
            raw = msg.get("usage")
        if not isinstance(raw, dict):
            return None
        usage = Usage(
            input_tokens=_as_int(raw.get("input_tokens"), 0),
            output_tokens=_as_int(raw.get("output_tokens"), 0),
            cache_read_tokens=_as_int(raw.get("cache_read_input_tokens"), 0),
            cache_write_tokens=_as_int(raw.get("cache_creation_input_tokens"), 0),
        )
        return UsagePayload(usage=usage, model="")

    def supports(
        self, blocks: Iterable[ContentBlock], caps: ProviderCapabilities
    ) -> None:
        if not caps.vision and any(isinstance(b, ImageBlock) for b in blocks):
            raise LLMError(
                "该模型未声明 vision 能力，无法处理图片输入。"
                "请改用声明 capabilities.vision=True 的模型，或移除图片块后重试。",
                kind=ErrorKind.UNSUPPORTED,
            )


__all__ = ["AnthropicWireCodec"]
