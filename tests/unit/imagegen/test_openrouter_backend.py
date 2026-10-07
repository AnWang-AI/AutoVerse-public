"""OpenRouterImageBackend — decode/save happy path + graceful failures (§11.7)."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import respx

from autoverse.infra.imagegen import ImageGenError, OpenRouterImageBackend

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")


class _Secrets:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, key: str) -> str | None:
        return self._value

    def require(self, key: str) -> str:
        assert self._value is not None
        return self._value


def _backend(tmp_path: Path, key: str | None = "sk-or-test") -> OpenRouterImageBackend:
    return OpenRouterImageBackend(_Secrets(key), save_dir=tmp_path)


def _images_response(data_url: str) -> dict[str, object]:
    return {"choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]}


@respx.mock
async def test_generates_and_saves_image(tmp_path: Path) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_images_response(f"data:image/png;base64,{_PNG_B64}")
        )
    )
    result = await _backend(tmp_path).generate(prompt="a cat", aspect_ratio="16:9")
    assert Path(result.path).exists()
    assert result.media_type == "image/png"
    assert result.byte_size > 0
    assert Path(result.path).read_bytes() == base64.b64decode(_PNG_B64)
    # base64_data round-trips to the same bytes for vision feedback (§11.7).
    assert base64.b64decode(result.base64_data) == base64.b64decode(_PNG_B64)


@respx.mock
async def test_falls_back_to_content_data_url(tmp_path: Path) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"data:image/webp;base64,{_PNG_B64}"}}]},
        )
    )
    result = await _backend(tmp_path).generate(prompt="x")
    assert result.media_type == "image/webp"
    assert Path(result.path).suffix == ".webp"


@respx.mock
async def test_explicit_save_path_respected(tmp_path: Path) -> None:
    target = tmp_path / "out" / "pic.png"
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_images_response(f"data:image/png;base64,{_PNG_B64}")
        )
    )
    result = await _backend(tmp_path).generate(prompt="x", save_path=str(target))
    assert Path(result.path) == target.resolve()
    assert target.exists()


async def test_missing_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ImageGenError, match="OPENROUTER_API_KEY"):
        await _backend(tmp_path, key=None).generate(prompt="x")


async def test_empty_prompt_raises(tmp_path: Path) -> None:
    with pytest.raises(ImageGenError, match="prompt"):
        await _backend(tmp_path).generate(prompt="   ")


@respx.mock
async def test_no_image_in_response_raises(tmp_path: Path) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {}}]})
    )
    with pytest.raises(ImageGenError, match="No image"):
        await _backend(tmp_path).generate(prompt="x")


@respx.mock
async def test_http_error_wrapped(tmp_path: Path) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(400, text="bad model"))
    with pytest.raises(ImageGenError, match="OpenRouter HTTP 400"):
        await _backend(tmp_path).generate(prompt="x")
