"""GenerateImageTool (docs/reference/tools.md §11.7): text-to-image via an injected backend.

PROCESS + network. This is a thin shell: the actual image-generation backend is
injected on ``ctx.app.image_backend`` (kept out of core, like the browser
backend). When no backend or no ``OPENROUTER_API_KEY`` is configured the tool
degrades gracefully with a clear, actionable error rather than crashing.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class GenerateImageArgs(BaseModel):
    prompt: str = Field(..., description="Text description of the image to generate.")
    model: str | None = Field(None, description="Optional backend model id.")
    aspect_ratio: str = Field("1:1", description="Aspect ratio, e.g. 1:1 / 16:9.")
    save_path: str | None = Field(None, description="Optional local path to save the image.")


@register_tool
class GenerateImageTool(BaseTool):
    name: ClassVar[str] = "generate_image"
    description: ClassVar[str] = "Generate an image from a text description."
    capability: ClassVar[CapabilityTier] = CapabilityTier.PROCESS
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = GenerateImageArgs

    async def run(self, args: GenerateImageArgs, ctx: ToolContext) -> ToolResult:
        backend: Any = getattr(ctx.app, "image_backend", None)
        if backend is None:
            return ToolResult.error(
                "Image generation is not configured. Provide an image backend on "
                "AppContext.image_backend (and set OPENROUTER_API_KEY), or remove "
                "the generate_image call.",
                error_kind="no_backend",
            )
        # Resolve the key via the secret provider when present, else env.
        if not self._has_key(ctx):
            return ToolResult.error(
                "OPENROUTER_API_KEY is not set; cannot generate images.",
                error_kind="no_credential",
            )
        try:
            result = await backend.generate(
                prompt=args.prompt,
                model=args.model,
                aspect_ratio=args.aspect_ratio,
                save_path=args.save_path,
            )
        except Exception as exc:  # noqa: BLE001 — surface as actionable error, never crash
            return ToolResult.error(
                f"Image generation failed: {exc}", error_kind="backend_error"
            )
        path = getattr(result, "path", None) or (result if isinstance(result, str) else "")
        meta: dict[str, object] = {"path": str(path)}
        # Surface the model the backend actually used (it may have resolved a
        # default when ``args.model`` was omitted) so the TUI can show it, mirroring
        # read_image's ``· {model}`` suffix.
        model = str(getattr(result, "model", "") or args.model or "")
        if model:
            meta["model"] = model
        # Surface the raw base64 + media type so the loop can lift the image back
        # into the conversation as a vision block for vision-capable models, and
        # frontends can preview it (docs/reference/tools.md §11.7). Large base64 lives in
        # metadata, never in ``output`` (§3.3 — keep the context lean).
        b64 = getattr(result, "base64_data", "")
        if b64:
            meta["images"] = [b64]
            meta["image_media_type"] = getattr(result, "media_type", "image/png")
        return ToolResult.ok(f"Generated image: {path}", **meta)

    @staticmethod
    def _has_key(ctx: ToolContext) -> bool:
        secrets = getattr(ctx.app, "secrets", None)
        if secrets is not None:
            getter = getattr(secrets, "get", None)
            if callable(getter):
                try:
                    if getter("OPENROUTER_API_KEY"):
                        return True
                except Exception:  # noqa: BLE001
                    pass
        return bool(os.environ.get("OPENROUTER_API_KEY"))


__all__ = ["GenerateImageTool"]
