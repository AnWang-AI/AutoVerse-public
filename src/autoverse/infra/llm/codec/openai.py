"""OpenAI wire codec (docs/reference/models.md §5.1, §9.1, §9.2, §10).

Pure, network-free. tool_calls dialect: assistant ToolUseBlocks collapse into
``message.tool_calls[]``; ToolResultBlocks become standalone ``{"role":"tool"}``
messages. Streaming tool-call fragments accumulate per index in DecodeState and
emit TOOL_START on finish (TOOL_END is a core/tools artifact).
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


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


class OpenAIWireCodec:
    wire: WireKind = WireKind.OPENAI

    def encode_messages(self, messages: list[Message]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for msg in messages:
            text_parts: list[str] = []
            image_parts: list[dict[str, object]] = []
            tool_calls: list[dict[str, object]] = []
            tool_results: list[dict[str, object]] = []
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text_parts.append(b.text)
                elif isinstance(b, ThinkingBlock):
                    # thinking is not re-encoded into the request body.
                    continue
                elif isinstance(b, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": b.id,
                            "type": "function",
                            "function": {
                                "name": b.name,
                                "arguments": json.dumps(b.input),
                            },
                        }
                    )
                elif isinstance(b, ToolResultBlock):
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": b.tool_use_id,
                            "content": b.content,
                        }
                    )
                elif isinstance(b, ImageBlock):
                    image_parts.append(self._encode_image(b))
            # OpenAI requires ``tool`` messages to directly follow the assistant
            # ``tool_calls`` message. Whenever a message mixes tool results with
            # sibling text/image parts (e.g. an interrupted tool whose
            # placeholder result was merged with the user's next prompt, or a
            # tool result carrying an image on a sibling ``user`` message per
            # §11.7), the tool results MUST be emitted first; otherwise the
            # intervening user/text message breaks the tool_calls→tool adjacency
            # and the backend rejects the request with HTTP 400.
            tools_first = bool(tool_results and (text_parts or image_parts))
            if tools_first:
                out.extend(tool_results)
            if text_parts or tool_calls or image_parts:
                message: dict[str, object] = {"role": msg.role}
                if image_parts:
                    # OpenAI multimodal: content becomes a parts array mixing
                    # text and image_url entries (§10). Text-only messages keep
                    # the simpler string form.
                    parts: list[dict[str, object]] = []
                    joined = "".join(text_parts)
                    if joined:
                        parts.append({"type": "text", "text": joined})
                    parts.extend(image_parts)
                    message["content"] = parts
                else:
                    message["content"] = "".join(text_parts)
                if tool_calls:
                    message["tool_calls"] = tool_calls
                out.append(message)
            if not tools_first:
                out.extend(tool_results)
        return out

    @staticmethod
    def _encode_image(b: ImageBlock) -> dict[str, object]:
        from autoverse.infra.llm.codec.image import clamp_image_base64

        if b.source_type == "url":
            url = b.data
        else:
            media_type, data = clamp_image_base64(b.media_type, b.data)
            url = f"data:{media_type};base64,{data}"
        return {"type": "image_url", "image_url": {"url": url}}

    def encode_tools(self, tools: list[ToolSchema]) -> list[dict[str, object]]:
        from autoverse.infra.llm.codec.base import sanitize_top_level

        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": sanitize_top_level(t.json_schema),
                },
            }
            for t in tools
        ]

    def encode_system(self, system: str, *, cache: bool) -> object:
        # OpenAI wire conveys system as a message; cache is not injected here.
        return system

    def decode_chunk(self, chunk: dict[str, object], st: DecodeState) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        choices = _as_list(chunk.get("choices"))
        finish_reason: str | None = None
        for raw_choice in choices:
            choice = _as_dict(raw_choice)
            delta = _as_dict(choice.get("delta"))
            content = delta.get("content")
            if content:
                events.append(make_event(TextDeltaPayload(text=str(content))))
            # OpenAI/DeepSeek-native uses ``reasoning_content``; OpenRouter
            # normalises it to ``reasoning``. Accept either.
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                events.append(
                    make_event(ThinkingDeltaPayload(text=str(reasoning)))
                )
            for raw_tc in _as_list(delta.get("tool_calls")):
                tc = _as_dict(raw_tc)
                index = _as_int(tc.get("index"), 0)
                slot = st.tool(index)
                if tc.get("id"):
                    slot.id = str(tc["id"])
                fn = _as_dict(tc.get("function"))
                if fn.get("name"):
                    slot.name = str(fn["name"])
                if fn.get("arguments"):
                    slot.args_parts.append(str(fn["arguments"]))
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

        usage = self._extract_usage(chunk)
        if usage is not None:
            events.append(make_event(usage))

        # Some backends (e.g. DeepSeek-V4 via OpenRouter) send TWO terminal
        # chunks both carrying ``finish_reason`` — the first with the content
        # delta, the second with the trailing ``usage`` block. TURN_END is the
        # terminal event of a turn and must be emitted exactly once; suppress a
        # repeat while still surfacing the late USAGE event above.
        if finish_reason is not None and not st.turn_ended:
            st.turn_ended = True
            st.finish_reason = finish_reason
            if finish_reason == "tool_calls":
                for slot in st.finished_tools():
                    raw = "".join(slot.args_parts) or "{}"
                    try:
                        parsed_obj = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        parsed_obj = {}
                    parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
                    events.append(
                        make_event(
                            ToolStartPayload(
                                tool_use_id=slot.id,
                                tool_name=slot.name,
                                tool_input=parsed,
                            )
                        )
                    )
            events.append(
                make_event(
                    TurnEndPayload(
                        turn_index=0,
                        iterations=0,
                        stop_cause=normalize_stop_reason(finish_reason),
                    )
                )
            )
        return events

    def _extract_usage(self, chunk: dict[str, object]) -> UsagePayload | None:
        raw = chunk.get("usage")
        if not isinstance(raw, dict):
            return None
        details = raw.get("prompt_tokens_details", {})
        cached = 0
        cache_write = 0
        if isinstance(details, dict):
            cached = _as_int(details.get("cached_tokens"), 0)
            # Some OpenRouter providers (e.g. native DeepSeek) surface a separate
            # cache-write counter; default 0 when absent.
            cache_write = _as_int(details.get("cache_write_tokens"), 0)
        # Official DeepSeek (api.deepseek.com) reports its automatic prefix-cache
        # hit at the top level as ``prompt_cache_hit_tokens`` (with a matching
        # ``prompt_cache_miss_tokens``). It usually also mirrors the hit into
        # ``prompt_tokens_details.cached_tokens``; fall back to the top-level
        # counter when the details bucket is missing/zero so cache reads are not
        # silently billed at full input price.
        if cached == 0:
            cached = _as_int(raw.get("prompt_cache_hit_tokens"), 0)
        prompt_tokens = _as_int(raw.get("prompt_tokens"), 0)
        # OpenAI-wire ``prompt_tokens`` is the *full* prompt including the cached
        # prefix, whereas ``cached_tokens`` is a subset of it. Internal Usage
        # keeps the three buckets disjoint (matching the Anthropic codec), so the
        # cached read is subtracted from ``input_tokens``. Without this the meter
        # would bill the cached tokens at input price *and* cache-read price, and
        # the sidebar token counter would double-count them. Clamp at zero in case
        # a provider reports cached > prompt.
        input_tokens = max(prompt_tokens - cached, 0)
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=_as_int(raw.get("completion_tokens"), 0),
            cache_read_tokens=cached,
            cache_write_tokens=cache_write,
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


__all__ = ["OpenAIWireCodec"]
