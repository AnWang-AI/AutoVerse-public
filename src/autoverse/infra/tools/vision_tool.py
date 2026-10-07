"""VisionTool — let text-only models "see" by delegating to a multimodal model.

A text-only main model (DeepSeek, GLM, …) cannot accept ``ImageBlock`` input —
the OpenAI codec gates non-vision models and raises. This tool bridges that gap:
it makes a single, low-temperature one-shot call to a *vision-capable* model
(default ``openrouter/minimax-m3``) about one image (local file or URL) and returns
the model's **text** answer. The result flows back to the caller as plain text,
so it never re-triggers the vision gate on the caller's own model.

Like :class:`AgentTool`, this is a thin infra shell: it is NOT
``@register_tool``-decorated. The app layer constructs it with a provider factory
(``Callable[[str], LLMProvider]``, typically ``Router(...).build``) and registers
it at runtime. ``infra → core`` / ``infra → ports`` are legal directions (P3).
"""

from __future__ import annotations

import base64
import os
from pathlib import PurePosixPath
from typing import Callable, ClassVar

from pydantic import BaseModel, Field, model_validator

_MAX_IMAGES = 8

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.ports import LLMProvider
from autoverse.types.abort import AbortError
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import EventType, TextDeltaPayload
from autoverse.types.llm import LLMRequest
from autoverse.types.messages import ImageBlock, Message, TextBlock
from autoverse.types.tools import ToolResult

_DEFAULT_MODEL = "openrouter/minimax-m3"
_DEFAULT_QUESTION = "Describe this image in detail."
_SYSTEM = (
    "You are a precise vision assistant. Answer strictly and only about the "
    "provided image. Be concrete and concise; do not invent details that are "
    "not visible."
)
# Map common image extensions → MIME media types (OpenAI image_url wire).
_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _media_type_for(name: str) -> str:
    return _MEDIA_TYPES.get(PurePosixPath(name).suffix.lower(), "image/png")


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


class ReadImageArgs(BaseModel):
    sources: list[str] = Field(
        ...,
        description="One or more image sources to analyze together in a single "
        "request: local file paths (relative to cwd or absolute) and/or http(s) "
        "URLs, freely mixed. Provide at least one; the vision model sees all of "
        f"them at once (max {_MAX_IMAGES}).",
    )
    question: str = Field(
        _DEFAULT_QUESTION,
        description="What to ask about the image(s) (e.g. 'transcribe the text', "
        "'what differs between these screenshots?'). Defaults to a detailed "
        "description.",
    )
    model: str = Field(
        "",
        description="Optional override for the multimodal model id; defaults to "
        "openrouter/minimax-m3.",
    )

    @model_validator(mode="after")
    def _check_sources(self) -> "ReadImageArgs":
        cleaned = [s.strip() for s in self.sources if s and s.strip()]
        if not cleaned:
            raise ValueError("Provide at least one image source in 'sources'.")
        if len(cleaned) > _MAX_IMAGES:
            raise ValueError(
                f"Too many images: {len(cleaned)} (max {_MAX_IMAGES} per call)."
            )
        object.__setattr__(self, "sources", cleaned)
        return self


class VisionTool(BaseTool):
    name: ClassVar[str] = "read_image"
    description: ClassVar[str] = (
        "Read/analyze one or more images by delegating to a multimodal model. Use "
        "this when you need to understand local image files or image URLs (e.g. "
        "screenshots, diagrams, photos) but your own model is text-only. Pass a "
        "'sources' list of local paths and/or http(s) URLs (mixed is fine) plus an "
        "optional 'question'; all images are sent together so the model can compare "
        "or summarize across them. Returns the vision model's text answer."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = ReadImageArgs

    def __init__(
        self,
        provider_for: Callable[[str], LLMProvider],
        *,
        default_model: str = _DEFAULT_MODEL,
    ) -> None:
        self._provider_for = provider_for
        self._default_model = default_model
        # One provider instance per model id (the underlying transport/httpx is
        # reusable across calls); built lazily on first use.
        self._cache: dict[str, LLMProvider] = {}

    def _provider(self, model_id: str) -> LLMProvider:
        cached = self._cache.get(model_id)
        if cached is None:
            cached = self._provider_for(model_id)
            self._cache[model_id] = cached
        return cached

    @staticmethod
    def _has_key(ctx: ToolContext) -> bool:
        secrets = getattr(ctx.app, "secrets", None)
        getter = getattr(secrets, "get", None)
        if callable(getter):
            try:
                if getter("OPENROUTER_API_KEY"):
                    return True
            except Exception:  # noqa: BLE001 — fall through to env
                pass
        return bool(os.environ.get("OPENROUTER_API_KEY"))

    @staticmethod
    def _build_image_block(source: str, ctx: ToolContext) -> ImageBlock:
        if _is_url(source):
            return ImageBlock(media_type="image/*", data=source, source_type="url")
        resolved = ctx.resolve_path(source)
        raw = resolved.read_bytes()  # raises FileNotFoundError/OSError → caught in run()
        b64 = base64.b64encode(raw).decode("ascii")
        return ImageBlock(media_type=_media_type_for(resolved.name), data=b64)

    async def run(self, args: ReadImageArgs, ctx: ToolContext) -> ToolResult:
        model_id = args.model or self._default_model
        if not self._has_key(ctx):
            return ToolResult.error(
                "Vision delegation is unavailable: OPENROUTER_API_KEY is not set. "
                "Set it (env or vault) to use read_image.",
                error_kind="no_credential",
            )

        images: list[ImageBlock] = []
        for source in args.sources:
            try:
                images.append(self._build_image_block(source, ctx))
            except FileNotFoundError:
                return ToolResult.error(
                    f"Image file not found: {source}", error_kind="not_found"
                )
            except OSError as exc:
                return ToolResult.error(
                    f"Could not read image '{source}': {exc}", error_kind="read_error"
                )

        req = LLMRequest(
            messages=(
                Message(
                    role="user",
                    content=(
                        TextBlock(text=args.question or _DEFAULT_QUESTION),
                        *images,
                    ),
                ),
            ),
            system=_SYSTEM,
            model=model_id,
            max_tokens=1024,
            temperature=0.0,
        )

        try:
            provider = self._provider(model_id)
            chunks: list[str] = []
            async for event in provider.stream(req, ctx.signal):
                if event.type is EventType.TEXT_DELTA and isinstance(
                    event.payload, TextDeltaPayload
                ):
                    chunks.append(event.payload.text)
        except AbortError:
            raise  # honoured by the dispatch layer, never swallowed
        except Exception as exc:  # noqa: BLE001 — surface as actionable error, never crash
            return ToolResult.error(
                f"Vision model '{model_id}' failed: {exc}", error_kind="vision_error"
            )

        text = "".join(chunks).strip()
        if not text:
            return ToolResult.error(
                f"Vision model '{model_id}' returned an empty response.",
                error_kind="empty_response",
            )
        # Metadata feeds the TUI card (title + expandable detail rows): the
        # delegated model, every image source, the count, and the question asked.
        return ToolResult.ok(
            text,
            model=model_id,
            sources=list(args.sources),
            source=args.sources[0] if args.sources else "",
            count=len(images),
            question=args.question or _DEFAULT_QUESTION,
            chars=len(text),
        )


__all__ = ["VisionTool", "ReadImageArgs"]
