"""Unit coverage for VisionTool — the multimodal delegation bridge.

Verifies the one-shot delegate path (text-only caller → vision model → text
answer), source validation (exactly one of path/url), graceful degradation
(no credential, missing file, empty/failed response), and that an ImageBlock is
actually placed on the delegated request.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from autoverse.infra.tools.vision_tool import ReadImageArgs, VisionTool
from autoverse.types.events import EventType, TextDeltaPayload, make_event
from autoverse.types.messages import ImageBlock, TextBlock

from tests._doubles import ScriptedProvider
from tests.conftest import make_ctx

# A 1x1 transparent PNG (valid bytes so the optional Pillow clamp path is happy).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _text_turn(*parts: str) -> list[object]:
    return [make_event(TextDeltaPayload(text=p)) for p in parts]


@pytest.fixture(autouse=True)
def _with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # _has_key falls back to env when AppContext.secrets is None (make_ctx).
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


# ── argument validation ───────────────────────────────────────────────────────

def test_requires_at_least_one_source() -> None:
    with pytest.raises(ValueError):
        ReadImageArgs(sources=[])  # empty
    with pytest.raises(ValueError):
        ReadImageArgs(sources=["  ", ""])  # only blanks


def test_rejects_too_many_sources() -> None:
    with pytest.raises(ValueError):
        ReadImageArgs(sources=[f"img{i}.png" for i in range(9)])  # > _MAX_IMAGES (8)


# ── happy path (local file) ───────────────────────────────────────────────────

async def test_local_image_delegates_and_returns_text(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[_text_turn("A red ", "square.")])
    tool = VisionTool(lambda mid: provider, default_model="minimax/minimax-m3")
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["shot.png"], question="what?"), ctx)

    assert not out.is_error
    assert out.output == "A red square."
    assert out.metadata["model"] == "minimax/minimax-m3"
    assert out.metadata["source"] == "shot.png"
    assert out.metadata["sources"] == ["shot.png"]
    assert out.metadata["count"] == 1
    # The delegated request carried the text question + a base64 ImageBlock.
    assert len(provider.requests) == 1
    blocks = provider.requests[0].messages[0].content
    assert any(isinstance(b, TextBlock) and b.text == "what?" for b in blocks)
    img_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    assert len(img_blocks) == 1
    assert img_blocks[0].source_type == "base64"
    assert img_blocks[0].media_type == "image/png"


async def test_model_override_is_honoured(tmp_path: Path) -> None:
    img = tmp_path / "x.jpg"
    img.write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[_text_turn("ok")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(
        ReadImageArgs(sources=["x.jpg"], model="moonshotai/kimi-k2.7-code"), ctx
    )
    assert not out.is_error
    assert out.metadata["model"] == "moonshotai/kimi-k2.7-code"
    assert provider.requests[0].model == "moonshotai/kimi-k2.7-code"


# ── happy path (URL) ──────────────────────────────────────────────────────────

async def test_url_source_builds_url_image_block(tmp_path: Path) -> None:
    provider = ScriptedProvider(scripts=[_text_turn("a cat")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(
        ReadImageArgs(sources=["https://example.com/cat.jpg"], question="?"), ctx
    )
    assert not out.is_error
    assert out.metadata["source"] == "https://example.com/cat.jpg"
    img_blocks = [b for b in provider.requests[0].messages[0].content
                  if isinstance(b, ImageBlock)]
    assert img_blocks[0].source_type == "url"
    assert img_blocks[0].data == "https://example.com/cat.jpg"


# ── multiple images (mixed local + URL in one request) ────────────────────────

async def test_multiple_mixed_sources_in_one_request(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(_PNG_1X1)
    (tmp_path / "b.jpg").write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[_text_turn("They differ in colour.")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(
        ReadImageArgs(
            sources=["a.png", "b.jpg", "https://x/c.webp"],
            question="what differs?",
        ),
        ctx,
    )
    assert not out.is_error
    assert out.metadata["count"] == 3
    assert out.metadata["sources"] == ["a.png", "b.jpg", "https://x/c.webp"]
    # All three images ride in a SINGLE user message, after the question text.
    blocks = provider.requests[0].messages[0].content
    img_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    assert len(img_blocks) == 3
    kinds = [b.source_type for b in img_blocks]
    assert kinds == ["base64", "base64", "url"]


async def test_one_missing_file_fails_whole_call(tmp_path: Path) -> None:
    (tmp_path / "ok.png").write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[_text_turn("never")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["ok.png", "gone.png"]), ctx)
    assert out.is_error and out.error_kind == "not_found"
    assert "gone.png" in out.output
    assert not provider.requests  # never reached the model


# ── graceful degradation ──────────────────────────────────────────────────────

async def test_missing_credential_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    provider = ScriptedProvider(scripts=[_text_turn("never")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["https://x/y.png"]), ctx)
    assert out.is_error and out.error_kind == "no_credential"
    assert not provider.requests  # never reached the model


async def test_missing_file_degrades(tmp_path: Path) -> None:
    provider = ScriptedProvider(scripts=[_text_turn("never")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["nope.png"]), ctx)
    assert out.is_error and out.error_kind == "not_found"
    assert not provider.requests


async def test_empty_response_degrades(tmp_path: Path) -> None:
    img = tmp_path / "e.png"
    img.write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[[]])  # provider yields nothing
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["e.png"]), ctx)
    assert out.is_error and out.error_kind == "empty_response"


async def test_provider_failure_degrades(tmp_path: Path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(_PNG_1X1)
    provider = ScriptedProvider(scripts=[RuntimeError("upstream 500")])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["f.png"]), ctx)
    assert out.is_error and out.error_kind == "vision_error"
    assert "upstream 500" in out.output


# ── schema / registration shape ───────────────────────────────────────────────

def test_tool_metadata() -> None:
    assert VisionTool.name == "read_image"
    assert VisionTool.needs_network is True
    schema = VisionTool.to_api_schema()
    assert schema["name"] == "read_image"
    props = schema["input_schema"]["properties"]
    assert {"sources", "question", "model"} <= set(props)


async def test_event_type_filtering(tmp_path: Path) -> None:
    """Only TEXT_DELTA payloads are collected; other events are ignored."""
    img = tmp_path / "g.png"
    img.write_bytes(_PNG_1X1)
    # Interleave a non-text event between the text deltas.
    from autoverse.types.events import ThinkingDeltaPayload

    turn: list[object] = [
        make_event(TextDeltaPayload(text="hello ")),
        make_event(ThinkingDeltaPayload(text="(thinking)")),
        make_event(TextDeltaPayload(text="world")),
    ]
    provider = ScriptedProvider(scripts=[turn])
    tool = VisionTool(lambda mid: provider)
    ctx = make_ctx(tmp_path)

    out = await tool.run(ReadImageArgs(sources=["g.png"]), ctx)
    assert not out.is_error
    assert out.output == "hello world"
    assert EventType.TEXT_DELTA  # sanity: enum import used
