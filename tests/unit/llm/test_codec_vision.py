"""gap2 — multimodal send (docs/reference/models.md §10/§14).

Verifies both wire codecs actually encode ImageBlock (OpenAI image_url /
Anthropic image source) when vision is declared, that oversize images are
clamped, and that the vision=False rejection path is unchanged (M6 regression).
"""

from __future__ import annotations

import base64
import io
from typing import Any, cast

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.image import MAX_DIMENSION, clamp_image_base64
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.taxonomy import LLMError
from autoverse.types import (
    ImageBlock,
    Message,
    ProviderCapabilities,
    TextBlock,
    ToolResultBlock,
)

import pytest

_PNG_1x1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


def _png_of_size(w: int, h: int) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── OpenAI wire ───────────────────────────────────────────────────────────────

def test_openai_encodes_image_url_when_vision() -> None:
    codec = OpenAIWireCodec()
    msg = Message(
        role="user",
        content=(TextBlock(text="what is this?"), ImageBlock(media_type="image/png", data=_PNG_1x1)),
    )
    out = codec.encode_messages([msg])
    content = out[0]["content"]
    assert isinstance(content, list)
    kinds = [p["type"] for p in content]
    assert "text" in kinds and "image_url" in kinds
    img = next(p for p in content if p["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/")
    assert "base64," in img["image_url"]["url"]


def test_openai_image_url_source_passthrough() -> None:
    codec = OpenAIWireCodec()
    msg = Message(
        role="user",
        content=(ImageBlock(media_type="image/png", data="https://x/y.png", source_type="url"),),
    )
    content = codec.encode_messages([msg])[0]["content"]
    img = next(p for p in content if p["type"] == "image_url")
    assert img["image_url"]["url"] == "https://x/y.png"


# ── tool_result + image vision feedback (generate_image, §11.7) ──────────────

def test_openai_tool_result_image_emits_tool_before_user_image() -> None:
    """OpenAI: the ``tool`` result must precede the sibling ``user`` image."""
    codec = OpenAIWireCodec()
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(tool_use_id="call_1", content="Generated image: /tmp/x.png"),
            ImageBlock(media_type="image/png", data=_PNG_1x1),
        ),
    )
    out = codec.encode_messages([msg])
    assert [m["role"] for m in out] == ["tool", "user"]
    assert out[0]["tool_call_id"] == "call_1"
    parts = cast(list[dict[str, Any]], out[1]["content"])
    assert any(p["type"] == "image_url" for p in parts)


def test_openai_plain_tool_result_unchanged() -> None:
    """No image → a lone ``tool`` message (ordering change must not regress)."""
    codec = OpenAIWireCodec()
    msg = Message(role="user", content=(ToolResultBlock(tool_use_id="c", content="ok"),))
    out = codec.encode_messages([msg])
    assert len(out) == 1 and out[0]["role"] == "tool"


def test_anthropic_tool_result_image_same_user_message() -> None:
    codec = AnthropicWireCodec()
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(tool_use_id="call_1", content="Generated image: /tmp/x.png"),
            ImageBlock(media_type="image/png", data=_PNG_1x1),
        ),
    )
    out = codec.encode_messages([msg])
    assert len(out) == 1 and out[0]["role"] == "user"
    blocks = cast(list[dict[str, Any]], out[0]["content"])
    assert [b["type"] for b in blocks] == ["tool_result", "image"]


# ── Anthropic wire ────────────────────────────────────────────────────────────

def test_anthropic_encodes_image_source_when_vision() -> None:
    codec = AnthropicWireCodec()
    msg = Message(role="user", content=(ImageBlock(media_type="image/png", data=_PNG_1x1),))
    out = codec.encode_messages([msg])
    # find the image block in the content array
    content = out[0]["content"]
    img = next(b for b in content if isinstance(b, dict) and b.get("type") == "image")
    assert img["source"]["type"] == "base64"
    assert img["source"]["data"]


# ── oversize clamp ────────────────────────────────────────────────────────────

def test_clamp_downscales_oversize_image() -> None:
    big = _png_of_size(MAX_DIMENSION + 800, 100)
    mt, data = clamp_image_base64("image/png", big)
    assert mt == "image/jpeg"  # re-encoded
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(data)))
    assert max(img.size) <= MAX_DIMENSION


def test_clamp_passes_through_small_image() -> None:
    mt, data = clamp_image_base64("image/png", _PNG_1x1)
    assert mt == "image/png" and data == _PNG_1x1  # unchanged


def test_clamp_passes_through_undecodable() -> None:
    mt, data = clamp_image_base64("image/png", "not-base64!!!")
    assert mt == "image/png" and data == "not-base64!!!"


# ── vision=False rejection unchanged (M6 regression guard) ───────────────────

def test_vision_false_still_rejects_openai() -> None:
    codec = OpenAIWireCodec()
    caps = ProviderCapabilities(vision=False)
    with pytest.raises(LLMError):
        codec.supports([ImageBlock(media_type="image/png", data=_PNG_1x1)], caps)


def test_vision_false_still_rejects_anthropic() -> None:
    codec = AnthropicWireCodec()
    caps = ProviderCapabilities(vision=False)
    with pytest.raises(LLMError):
        codec.supports([ImageBlock(media_type="image/png", data=_PNG_1x1)], caps)
