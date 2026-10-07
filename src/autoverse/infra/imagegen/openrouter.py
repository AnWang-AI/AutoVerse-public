"""OpenRouter text-to-image backend (docs/reference/tools.md §11.7).

The ``generate_image`` tool is a thin shell; the real work lives here. We call
``POST {base_url}/chat/completions`` with OpenRouter's ``modalities`` extension —
image-generation models return the result as a base64 data URL inside
``choices[0].message.images[0].image_url.url``. The image is decoded, saved to
disk and returned as an :class:`ImageResult` (``.path``).

Network + filesystem only; no ``core`` import (P6 keeps the backend in ``infra``).
Credentials resolve via the injected :class:`SecretProvider` first, then the
environment — the same ``OPENROUTER_API_KEY`` used by the LLM transport.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from autoverse.ports import SecretProvider

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "bytedance-seed/seedream-4.5"
_DEFAULT_SAVE_DIR = Path("~/.autoverse/generated_images").expanduser()
_API_KEY_ALIAS = "OPENROUTER_API_KEY"
_TIMEOUT_S = 120.0

# model id → human-readable label for picker/display surfaces.
SUPPORTED_MODELS: dict[str, str] = {
    "bytedance-seed/seedream-4.5": "ByteDance Seedream 4.5",
    "black-forest-labs/flux-2-pro": "FLUX 2 Pro",
    "black-forest-labs/flux-2-klein-4b": "FLUX 2 Klein 4B",
    "x-ai/grok-imagine": "xAI Grok Imagine",
    "google/gemini-2.5-flash-image": "Gemini 2.5 Flash Image",
    "google/gemini-3-pro-image-preview": "Gemini 3 Pro Image Preview",
    "openai/gpt-5.4-image-2": "GPT-5.4 Image 2",
}

# Models that emit both image and text output (Gemini multimodal family).
_MULTIMODAL_OUTPUT_MODELS: frozenset[str] = frozenset(
    {
        "google/gemini-2.5-flash-image",
        "google/gemini-3-pro-image-preview",
        "google/gemini-3.1-flash-image-preview",
    }
)

_SUPPORTED_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)
_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class ImageGenError(Exception):
    """Raised on any image-generation failure.

    The ``generate_image`` tool shell catches this (and any other exception) and
    surfaces it as a ``ToolResult.error`` — the backend never crashes the loop.
    """


@dataclass(frozen=True)
class ImageResult:
    """A generated image already persisted to disk (docs/reference/tools.md §11.7).

    ``base64_data`` carries the raw (un-clamped) image so the loop can lift it
    back into the conversation as an :class:`ImageBlock` vision block for
    vision-capable models (the codec clamps oversize images at encode time).
    """

    path: str
    media_type: str
    model: str
    byte_size: int
    base64_data: str


def _model_slug(model: str) -> str:
    """Filesystem-safe short name for the model id."""
    return model.split("/")[-1][:40].replace(":", "-")


def _modalities(model: str) -> list[str]:
    return ["image", "text"] if model in _MULTIMODAL_OUTPUT_MODELS else ["image"]


class OpenRouterImageBackend:
    """Text-to-image via OpenRouter's multimodal chat-completions endpoint.

    ``client`` is injectable for tests; in production a short-lived
    :class:`httpx.AsyncClient` is created per call so the backend holds no
    long-lived connection (the tool is invoked rarely).
    """

    def __init__(
        self,
        secrets: SecretProvider | None = None,
        *,
        base_url: str = _OPENROUTER_BASE,
        save_dir: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self._secrets = secrets
        self._base_url = base_url.rstrip("/")
        self._save_dir = save_dir or _DEFAULT_SAVE_DIR
        self._client = client

    async def generate(
        self,
        *,
        prompt: str,
        model: str | None = None,
        aspect_ratio: str = "1:1",
        save_path: str | None = None,
    ) -> ImageResult:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageGenError("'prompt' is required and must be non-empty.")
        model = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
        aspect_ratio = (aspect_ratio or "1:1").strip() or "1:1"

        api_key = self._api_key()
        data = await self._post(prompt, model, aspect_ratio, api_key)
        data_url = _extract_data_url(data)
        media_type, raw = _decode_data_url(data_url)
        path = self._save(raw, media_type, model, save_path)
        return ImageResult(
            path=str(path),
            media_type=media_type,
            model=model,
            byte_size=len(raw),
            base64_data=base64.b64encode(raw).decode("ascii"),
        )

    def _api_key(self) -> str:
        if self._secrets is not None:
            try:
                value = self._secrets.get(_API_KEY_ALIAS)
            except Exception:  # noqa: BLE001 — provider failure → fall back to env
                value = None
            if value:
                return value
        env = os.environ.get(_API_KEY_ALIAS, "").strip()
        if not env:
            raise ImageGenError(
                f"{_API_KEY_ALIAS} is not set; cannot generate images."
            )
        return env

    async def _post(
        self, prompt: str, model: str, aspect_ratio: str, api_key: str
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — optional dependency
            raise ImageGenError("httpx is not installed (pip install httpx).") from exc

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": _modalities(model),
        }
        if aspect_ratio != "1:1":
            payload["image_config"] = {"aspect_ratio": aspect_ratio}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/autoverse",
            "X-Title": "AutoVerse",
        }
        url = f"{self._base_url}/chat/completions"

        try:
            if self._client is not None:
                resp = await self._client.post(url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            parsed = resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:400]
            raise ImageGenError(
                f"OpenRouter HTTP {exc.response.status_code}: {body}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ImageGenError(
                f"Image generation timed out after {_TIMEOUT_S:.0f}s; try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise ImageGenError(f"Image request failed: {exc}") from exc

        if not isinstance(parsed, dict):
            raise ImageGenError("Unexpected OpenRouter response (not a JSON object).")
        return parsed

    def _save(
        self, raw: bytes, media_type: str, model: str, save_path: str | None
    ) -> Path:
        if save_path:
            target = Path(save_path).expanduser().resolve()
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = _MIME_EXT.get(media_type, ".jpg")
            target = self._save_dir / f"{ts}_{_model_slug(model)}{ext}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as exc:
            raise ImageGenError(f"Failed to save image to {target}: {exc}") from exc
        return target


def _extract_data_url(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise ImageGenError(f"No choices in response: {str(data)[:300]}")
    message = choices[0].get("message") or {}
    images = message.get("images") or []
    if not images:
        # Some models put the data URL directly in `content`.
        content = message.get("content")
        if isinstance(content, str) and content.startswith("data:"):
            return content
        raise ImageGenError(f"No image in response: {str(message)[:300]}")
    try:
        url = images[0]["image_url"]["url"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ImageGenError(f"Malformed image in response: {exc}") from exc
    if not isinstance(url, str) or not url:
        raise ImageGenError("Empty image url in response.")
    return url


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    media_type = "image/jpeg"
    b64 = data_url
    if data_url.startswith("data:"):
        header, _, b64 = data_url.partition(",")
        mime = header[5:].split(";", 1)[0].strip()
        if mime in _SUPPORTED_MIMES:
            media_type = mime
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError(f"Failed to decode base64 image: {exc}") from exc
    return media_type, raw


__all__ = ["OpenRouterImageBackend", "ImageResult", "ImageGenError", "SUPPORTED_MODELS"]
