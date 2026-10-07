"""generate_image vision feedback (docs/reference/tools.md §11.7, docs/reference/models.md §10).

Tool-produced images (``metadata['images']``) are lifted into the tool_result
message as ImageBlocks only for vision-capable models; non-vision models get
text-only so the codec's supports() pre-check never rejects the turn.
"""

from __future__ import annotations

from typing import Any, cast

from autoverse.core.loop.turn_loop import TurnLoop, _result_image_blocks
from autoverse.types import ProviderCapabilities
from autoverse.types.messages import ImageBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from autoverse.types.tools import ToolResult


class _LLM:
    def __init__(self, vision: bool) -> None:
        self.capabilities = ProviderCapabilities(vision=vision)


def _loop(vision: bool) -> TurnLoop:
    dummy = cast(Any, object())
    return TurnLoop(
        llm=cast(Any, _LLM(vision)),
        conversation=dummy,
        events=dummy,
        compaction=dummy,
        recovery=dummy,
        gates=dummy,
        dispatch_tools=dummy,
        app=dummy,
    )


def _image_result() -> ToolResult:
    return ToolResult.ok(
        "Generated image: /tmp/x.png",
        path="/tmp/x.png",
        images=["aGVsbG8="],
        image_media_type="image/png",
    )


def test_vision_model_lifts_image_into_result_message() -> None:
    call = ToolUseBlock(id="c1", name="generate_image", input={})
    msg = _loop(vision=True)._to_result_blocks((call,), [_image_result()])
    kinds = [type(b).__name__ for b in msg.content]
    assert kinds == ["ToolResultBlock", "ImageBlock"]
    img = next(b for b in msg.content if isinstance(b, ImageBlock))
    assert img.media_type == "image/png" and img.data == "aGVsbG8="


def test_non_vision_model_keeps_text_only() -> None:
    call = ToolUseBlock(id="c1", name="generate_image", input={})
    msg = _loop(vision=False)._to_result_blocks((call,), [_image_result()])
    assert all(isinstance(b, ToolResultBlock) for b in msg.content)


def test_llm_text_override_replaces_persisted_image_for_request() -> None:
    original = Message(
        role="user",
        content=(TextBlock("see"), ImageBlock(media_type="image/png", data="QUJD")),
        metadata={"llm_text_override": "image says BUILD FAILED"},
    )

    [request_msg] = _loop(vision=False)._request_messages([original])

    assert request_msg.content == (TextBlock("image says BUILD FAILED"),)
    assert original.content[1] == ImageBlock(media_type="image/png", data="QUJD")


def test_llm_context_prefix_is_hidden_from_history_and_applies_to_all_models() -> None:
    original = Message(
        role="user",
        content=(TextBlock("continue the browser task"),),
        metadata={"llm_context_prefix": "[Browser state notice]\nActive: page-b"},
    )

    [text_request] = _loop(vision=False)._request_messages([original])
    [vision_request] = _loop(vision=True)._request_messages([original])

    expected = (
        TextBlock("[Browser state notice]\nActive: page-b"),
        TextBlock("continue the browser task"),
    )
    assert text_request.content == expected
    assert vision_request.content == expected
    assert original.content == (TextBlock("continue the browser task"),)

    image_original = Message(
        role="user",
        content=(TextBlock("see"), ImageBlock(media_type="image/png", data="QUJD")),
        metadata={
            "llm_context_prefix": "[Browser state notice]\nActive: page-b",
            "llm_text_override": "image says BUILD FAILED",
        },
    )
    [image_request] = _loop(vision=False)._request_messages([image_original])
    assert image_request.content == (
        TextBlock("[Browser state notice]\nActive: page-b"),
        TextBlock("image says BUILD FAILED"),
    )


def test_result_image_blocks_skips_errors_and_malformed() -> None:
    err = ToolResult.error("boom", images=["aGVsbG8="])
    no_imgs = ToolResult.ok("ok")
    bad_shape = ToolResult.ok("ok", images="not-a-list")
    bad_items = ToolResult.ok("ok", images=[123, "", "Zm9v"])
    blocks = _result_image_blocks([err, no_imgs, bad_shape, bad_items])
    assert [b.data for b in blocks] == ["Zm9v"]
