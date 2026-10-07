"""Browser port shells and screenshot guards (docs/reference/tools.md §12, T12).

Concrete Playwright and native WebKit adapters live outside core. This module ships:
  - clamp_screenshot — pure guard that compresses/scales a screenshot under a
    150 KB budget (T12), degrading gracefully when Pillow is unavailable;
  - thin BrowserNavigate/Screenshot tools that delegate to ctx.app.browser.
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.core.tools.url_guard import check_url
from autoverse.ports import BrowserError, BrowserPort
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolCall, ToolResult

_JPEG_QUALITY_LADDER: tuple[int, ...] = (75, 60, 45, 30)
_MAX_SCREENSHOT_BYTES = 150_000
_MAX_IMAGE_DIMENSION = 2000
_MAX_PAGE_TEXT_PREVIEW_CHARS = 4_000
_MAX_PAGE_URL_CHARS = 2_048
_MAX_PAGE_TITLE_CHARS = 512


def _normalize_screenshot_policy(value: object) -> object:
    """Accept legacy booleans while exposing the new policy tokens in schema."""

    if isinstance(value, bool):
        return "always" if value else "never"
    return value


BrowserScreenshotPolicy = Annotated[
    Literal["auto", "always", "never"],
    BeforeValidator(_normalize_screenshot_policy),
]


# Backward-compatible names remain importable from this module while the single
# behavioral source of truth lives in ``autoverse.ports.browser``.
BrowserBackend = BrowserPort
BrowserBackendError = BrowserError


def clamp_screenshot(
    raw: bytes, *, last_good_quality: int = 75
) -> tuple[bytes | None, dict[str, Any]]:
    """Compress/scale `raw` under the 150 KB budget (T12).

    With Pillow: walk the JPEG quality ladder, scaling down if the image exceeds
    2000px on a side. Returns the first encoding <= 150 KB. If the whole ladder
    overshoots, returns (None, {reason: "screenshot too large"}).

    Without Pillow: pass through if already under the byte threshold, else
    (None, ...).
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow optional
        if len(raw) <= _MAX_SCREENSHOT_BYTES:
            return raw, {"quality": None, "engine": "passthrough", "bytes": len(raw)}
        return None, {
            "reason": "screenshot too large",
            "engine": "passthrough",
            "bytes": len(raw),
        }

    try:
        img: Any = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        return None, {"reason": f"undecodable image: {exc}", "engine": "pillow"}

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > _MAX_IMAGE_DIMENSION:
        scale = _MAX_IMAGE_DIMENSION / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    ladder = tuple(q for q in _JPEG_QUALITY_LADDER if q <= last_good_quality) or (
        _JPEG_QUALITY_LADDER[-1],
    )
    for quality in ladder:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= _MAX_SCREENSHOT_BYTES:
            return data, {"quality": quality, "engine": "pillow", "bytes": len(data)}

    return None, {"reason": "screenshot too large", "engine": "pillow"}


def _backend(ctx: ToolContext) -> BrowserBackend | None:
    backend = getattr(ctx.app, "browser", None)
    if isinstance(backend, BrowserPort):
        return backend
    return None


def _screenshot_metadata(
    data: bytes,
    meta: dict[str, Any],
    *,
    full_page: bool,
    selector: str | None,
) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "images": [encoded],
        "image_media_type": "image/jpeg",
        "full_page": full_page,
        "screenshot_selector": selector,
        **meta,
    }


async def _capture_screenshot_metadata(
    backend: BrowserBackend,
    *,
    full_page: bool = False,
    selector: str | None = None,
    page_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Best-effort screenshot capture for post-action visual feedback."""
    try:
        raw = await backend.screenshot(
            full_page=full_page, selector=selector, page_id=page_id
        )
    except BrowserBackendError as exc:
        return {"screenshot_error": str(exc), **exc.metadata}, ""
    data, meta = clamp_screenshot(raw)
    if data is None:
        return {"screenshot_error": meta.get("reason", "screenshot too large"), **meta}, ""
    shot_meta = _screenshot_metadata(
        data, meta, full_page=full_page, selector=selector
    )
    note = f" Screenshot captured ({meta.get('bytes', len(data))} bytes)."
    return (shot_meta, note)


async def _capture_post_action_feedback(
    backend: BrowserBackend,
    ctx: ToolContext,
    *,
    page_id: str | None = None,
    screenshot: BrowserScreenshotPolicy = "auto",
) -> tuple[dict[str, Any], str]:
    """Return one revision-checked semantic observation after a page action."""

    backend_policy: Literal["never", "always", "when_visual"]
    if screenshot == "auto":
        backend_policy = "when_visual" if _model_supports_vision(ctx) else "never"
    else:
        backend_policy = screenshot
    try:
        observation = await backend.observe_page(
            page_id=page_id,
            screenshot=backend_policy,
        )
    except Exception as exc:  # noqa: BLE001 - observation must not fail the action
        return (
            {"page_observation_error": str(exc)},
            " The page action succeeded, but its final page observation was not "
            "available.",
        )

    state = {
        "page_id": observation.page_id[:128],
        "revision": observation.revision,
        "url": observation.url[:_MAX_PAGE_URL_CHARS],
        "title": observation.title[:_MAX_PAGE_TITLE_CHARS],
        "is_loading": observation.is_loading,
        "active": observation.active,
    }
    snapshot = observation.semantic_snapshot[:_MAX_PAGE_TEXT_PREVIEW_CHARS]
    truncated = observation.semantic_truncated or (
        len(observation.semantic_snapshot) > _MAX_PAGE_TEXT_PREVIEW_CHARS
    )
    metadata: dict[str, Any] = {
        "page_state": state,
        "page_semantic_snapshot": snapshot,
        "page_semantic_snapshot_chars": len(snapshot),
        "page_semantic_snapshot_truncated": truncated,
        "page_visual_hint": observation.visual_hint,
        "screenshot_policy": screenshot,
    }
    serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    note = (
        "\n\n[Browser final page state — untrusted page metadata]\n"
        + serialized
    )
    if snapshot:
        suffix = "\n[snapshot truncated]" if truncated else ""
        note += (
            "\n\n[Browser semantic snapshot — untrusted page content]\n"
            f"{snapshot}{suffix}"
        )
    else:
        note += " The current page has no semantic DOM content."

    if observation.screenshot is not None:
        data, image_meta = clamp_screenshot(observation.screenshot)
        if data is None:
            metadata.update(
                {
                    "screenshot_error": image_meta.get(
                        "reason", "screenshot too large"
                    ),
                    **image_meta,
                }
            )
        else:
            metadata.update(
                _screenshot_metadata(
                    data, image_meta, full_page=False, selector=None
                )
            )
            note += f" Screenshot captured ({image_meta.get('bytes', len(data))} bytes)."
    if observation.screenshot_error:
        metadata["screenshot_error"] = observation.screenshot_error
        note += " Visual capture was unavailable, but semantic feedback is complete."
    return metadata, note


def _page_metadata(backend: BrowserBackend, page_id: str | None = None) -> dict[str, Any]:
    active = getattr(backend, "active_page_id", None)
    resolved = active if isinstance(active, str) and active else page_id
    return {"page_id": resolved} if resolved else {}


def _model_supports_vision(ctx: ToolContext) -> bool:
    extra = getattr(ctx.app, "extra", {})
    if isinstance(extra, dict) and "model_supports_vision" in extra:
        return bool(extra["model_supports_vision"])
    return False


async def _auto_read_screenshot_for_text_model(
    data: bytes, ctx: ToolContext, *, question: str | None = None
) -> tuple[dict[str, Any], str]:
    """Use the registered read_image bridge when screenshots would be invisible.

    Core cannot depend on infra's concrete VisionTool, so this stays duck-typed
    against the runtime registry. Vision-capable models still receive the image
    block through TurnLoop; text-only models get a plain-text visual summary.
    """
    if _model_supports_vision(ctx):
        return {}, ""

    tools = getattr(ctx.app, "tools", None)
    getter = getattr(tools, "get", None)
    read_image = getter("read_image") if callable(getter) else None
    if read_image is None:
        return {}, ""

    args_type = getattr(read_image, "Args", None)
    run = getattr(read_image, "run", None)
    if args_type is None or not callable(run):
        return {}, ""

    prompt = question or (
        "Describe this browser screenshot for a text-only model. Include visible "
        "text, UI state, layout, selected/focused controls, errors, and details "
        "that help decide the next browser action."
    )
    decision = await ctx.permissions.check(
        ToolCall(
            id="browser-auto-read-image",
            name="read_image",
            input={"sources": ["<browser-screenshot>"], "question": prompt},
            tier=CapabilityTier.READ_ONLY,
        )
    )
    if not decision.allowed:
        reason = decision.reason or "denied"
        return (
            {
                "image_description_error": f"read_image permission denied: {reason}",
                "image_description_auto": True,
            },
            " automatic read_image was denied by policy.",
        )

    try:
        with tempfile.TemporaryDirectory(prefix="autoverse-browser-shot-") as tmp:
            path = f"{tmp}/screenshot.jpg"
            with open(path, "wb") as fh:
                fh.write(data)
            result = await run(args_type(sources=[path], question=prompt), ctx)
    except Exception as exc:  # noqa: BLE001 — visual compatibility must degrade
        return (
            {
                "image_description_error": f"read_image compatibility failed: {exc}",
                "image_description_auto": True,
            },
            " read_image compatibility failed.",
        )

    if getattr(result, "is_error", False):
        output = str(getattr(result, "output", "") or "")
        return (
            {
                "image_description_error": output,
                "image_description_auto": True,
            },
            " read_image could not analyze the screenshot.",
        )

    output = str(getattr(result, "output", "") or "").strip()
    if not output:
        return (
            {
                "image_description_error": "read_image returned an empty response.",
                "image_description_auto": True,
            },
            " read_image returned an empty screenshot description.",
        )
    meta = dict(getattr(result, "metadata", {}) or {})
    model = meta.get("model")
    suffix = f" via read_image ({model})" if model else " via read_image"
    return (
        {
            "image_description": output,
            "image_description_auto": True,
            "image_description_model": model or "",
        },
        f"\n\n[Browser screenshot description{suffix}]\n{output}",
    )


class _BrowserPageTargetArgs(BaseModel):
    page_id: str | None = Field(
        None,
        min_length=1,
        description="Stable browser page id; defaults to the active page.",
    )


class BrowserListPagesArgs(BaseModel):
    pass


def _format_browser_page(page: dict[str, Any]) -> str:
    revision = f" [rev {page['revision']}]" if "revision" in page else ""
    loading = " [loading]" if page.get("is_loading") else ""
    return (
        f"{'*' if page.get('active') else '-'} "
        f"{page.get('page_id', '')}: {page.get('title') or 'Untitled'} "
        f"({page.get('url') or 'about:blank'}){revision}{loading}"
    )


@register_tool
class BrowserListPagesTool(BaseTool):
    name: ClassVar[str] = "browser_list_pages"
    description: ClassVar[str] = (
        "List open browser pages, the active page id, and loading state."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserListPagesArgs

    async def run(self, args: BrowserListPagesArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            pages = await backend.list_pages()
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        active = getattr(backend, "active_page_id", None)
        lines = [_format_browser_page(page) for page in pages]
        output = "\n".join(lines) if lines else "No browser pages are open."
        return ToolResult.ok(
            output,
            pages=pages,
            active_page_id=active,
            page_count=len(pages),
        )


class BrowserNewPageArgs(BaseModel):
    url: str | None = Field(None, description="Optional URL to open in the new page.")
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = Field(
        "domcontentloaded", description="Navigation wait condition when url is set."
    )
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserNewPageTool(BaseTool):
    name: ClassVar[str] = "browser_new_page"
    description: ClassVar[str] = "Create and activate a new browser page."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserNewPageArgs

    async def run(self, args: BrowserNewPageArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        if args.url is not None:
            ok, reason = check_url(
                args.url,
                allow_loopback=bool(getattr(backend, "allows_loopback", False)),
            )
            if not ok:
                return ToolResult.error(
                    f"Refusing to open {args.url}: {reason}", error_kind="blocked_url"
                )
        try:
            info = await backend.new_page(args.url, wait_until=args.wait_until)
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        page_id = str(info.get("page_id") or "") or None
        meta = {**info, **_page_metadata(backend, page_id)}
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Created browser page {page_id or 'unknown'}.{note}", **meta)


class BrowserSwitchPageArgs(BaseModel):
    page_id: str = Field(..., min_length=1, description="Page id to activate.")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserSwitchPageTool(BaseTool):
    name: ClassVar[str] = "browser_switch_page"
    description: ClassVar[str] = "Switch the active browser page by page id."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserSwitchPageArgs

    async def run(self, args: BrowserSwitchPageArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.switch_page(args.page_id)
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {**info, **_page_metadata(backend, args.page_id)}
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Switched to browser page {args.page_id}.{note}", **meta)


class BrowserClosePageArgs(BaseModel):
    page_id: str | None = Field(
        None, min_length=1, description="Page id to close; defaults to the active page."
    )


@register_tool
class BrowserClosePageTool(BaseTool):
    name: ClassVar[str] = "browser_close_page"
    description: ClassVar[str] = "Close one browser page without closing the browser session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserClosePageArgs

    async def run(self, args: BrowserClosePageArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.close_page(args.page_id)
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        closed = info.get("closed_page_id") or args.page_id or "active page"
        return ToolResult.ok(f"Closed browser page {closed}.", **info)


class BrowserNavigateArgs(_BrowserPageTargetArgs):
    url: str = Field(..., description="URL to navigate to.")
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = Field(
        "domcontentloaded", description="Navigation wait condition."
    )
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserNavigateTool(BaseTool):
    name: ClassVar[str] = "browser_navigate"
    description: ClassVar[str] = "Open a URL in the browser."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserNavigateArgs

    async def run(self, args: BrowserNavigateArgs, ctx: ToolContext) -> ToolResult:
        ok, reason = check_url(
            args.url,
            allow_loopback=bool(getattr(_backend(ctx), "allows_loopback", False)),
        )
        if not ok:
            return ToolResult.error(
                f"Refusing to navigate to {args.url}: {reason}",
                error_kind="blocked_url",
            )
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error(
                "No browser backend configured.", error_kind="no_backend"
            )
        try:
            info = await backend.navigate(
                args.url, wait_until=args.wait_until, page_id=args.page_id
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {"url": args.url, **info}  # backend info wins on key collision
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Navigated to {args.url}.{note}", **meta)


class BrowserScreenshotArgs(_BrowserPageTargetArgs):
    full_page: bool = Field(False, description="Capture the full scrollable page.")
    selector: str | None = Field(None, description="CSS selector for an element screenshot.")


@register_tool
class BrowserScreenshotTool(BaseTool):
    name: ClassVar[str] = "browser_screenshot"
    description: ClassVar[str] = "Capture a screenshot of the current page."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserScreenshotArgs

    async def run(self, args: BrowserScreenshotArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error(
                "No browser backend configured.", error_kind="no_backend"
            )
        try:
            raw = await backend.screenshot(
                full_page=args.full_page,
                selector=args.selector,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        data, meta = clamp_screenshot(raw)
        if data is None:
            return ToolResult.error(
                meta.get("reason", "screenshot too large"),
                error_kind="screenshot_too_large",
                **meta,
            )
        shot_meta = _screenshot_metadata(
            data, meta, full_page=args.full_page, selector=args.selector
        )
        read_meta, read_note = await _auto_read_screenshot_for_text_model(data, ctx)
        shot_meta.update(read_meta)
        shot_meta.update(_page_metadata(backend, args.page_id))
        return ToolResult.ok(
            f"Captured screenshot ({meta.get('bytes', len(data))} bytes).{read_note}",
            **shot_meta,
        )


def _require_backend(ctx: ToolContext) -> BrowserBackend | None:
    return _backend(ctx)


class BrowserClickArgs(_BrowserPageTargetArgs):
    selector: str | None = Field(None, description="CSS selector of the element to click.")
    x: float | None = Field(None, description="X pixel coordinate for coordinate clicks.")
    y: float | None = Field(None, description="Y pixel coordinate for coordinate clicks.")
    button: Literal["left", "right", "middle"] = Field(
        "left", description="Mouse button: left/right/middle."
    )
    double: bool = Field(False, description="Double-click instead of single.")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )

    @model_validator(mode="after")
    def _require_selector_or_coordinates(self) -> "BrowserClickArgs":
        has_selector = bool(self.selector)
        has_coords = self.x is not None and self.y is not None
        if has_selector == has_coords:
            raise ValueError("provide either selector or both x/y coordinates")
        return self


@register_tool
class BrowserClickTool(BaseTool):
    name: ClassVar[str] = "browser_click"
    description: ClassVar[str] = "Click an element by CSS selector or coordinates."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserClickArgs

    async def run(self, args: BrowserClickArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.click(
                args.selector,
                x=args.x,
                y=args.y,
                button=args.button,
                double=args.double,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {
            "selector": args.selector,
            "x": args.x,
            "y": args.y,
            "double": args.double,
            **info,
        }
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        target = args.selector or f"({args.x}, {args.y})"
        return ToolResult.ok(f"Clicked {target}.{note}", **meta)


class BrowserTypeArgs(_BrowserPageTargetArgs):
    selector: str = Field(..., description="CSS selector of the input element.")
    text: str = Field(..., description="Text to type.")
    clear: bool = Field(True, description="Clear the field before typing.")
    press_key: str | None = Field(None, description="Key to press after typing, e.g. Enter.")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserTypeTool(BaseTool):
    name: ClassVar[str] = "browser_type"
    description: ClassVar[str] = "Type text into an element identified by a CSS selector."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserTypeArgs

    async def run(self, args: BrowserTypeArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.type_text(
                args.selector,
                args.text,
                clear=args.clear,
                press_key=args.press_key,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {
            "selector": args.selector,
            "text": args.text,
            "press_key": args.press_key,
            **info,
        }
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Typed into {args.selector}.{note}", **meta)


class BrowserGetContentArgs(_BrowserPageTargetArgs):
    selector: str | None = Field(None, description="CSS selector to limit extraction.")
    mode: Literal["text", "html", "markdown"] = Field(
        "text", description="text/html/markdown."
    )


@register_tool
class BrowserGetContentTool(BaseTool):
    name: ClassVar[str] = "browser_get_content"
    description: ClassVar[str] = "Get text/HTML/markdown content of the current page."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserGetContentArgs

    async def run(self, args: BrowserGetContentArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            content = await backend.get_content(
                selector=args.selector, mode=args.mode, page_id=args.page_id
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        return ToolResult.ok(
            content,
            mode=args.mode,
            selector=args.selector,
            chars=len(content),
            **_page_metadata(backend, args.page_id),
        )


class BrowserScrollArgs(_BrowserPageTargetArgs):
    direction: Literal["down", "up", "left", "right"] = Field(
        "down", description="down/up/left/right."
    )
    amount: float = Field(500, ge=0, description="Pixels to scroll.")
    selector: str | None = Field(None, description="Element to scroll (default: window).")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserScrollTool(BaseTool):
    name: ClassVar[str] = "browser_scroll"
    description: ClassVar[str] = "Scroll the page or a specific element."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserScrollArgs

    async def run(self, args: BrowserScrollArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.scroll(
                direction=args.direction,
                amount=args.amount,
                selector=args.selector,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {
            "direction": args.direction,
            "amount": args.amount,
            "selector": args.selector,
            **info,
        }
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Scrolled {args.direction}.{note}", **meta)


class BrowserWaitArgs(_BrowserPageTargetArgs):
    condition: Literal[
        "visible", "hidden", "attached", "detached", "load", "domcontentloaded", "networkidle"
    ] = Field("visible", description="Element or page readiness condition.")
    selector: str | None = Field(None, description="CSS selector for element conditions.")
    timeout: float = Field(10000, ge=0, description="Max wait in ms.")

    @model_validator(mode="after")
    def _selector_for_element_condition(self) -> "BrowserWaitArgs":
        if self.condition in {"visible", "hidden", "attached", "detached"} and not self.selector:
            raise ValueError(f"condition {self.condition!r} requires selector")
        return self


@register_tool
class BrowserWaitTool(BaseTool):
    name: ClassVar[str] = "browser_wait"
    description: ClassVar[str] = "Wait for an element/navigation/network condition."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserWaitArgs

    async def run(self, args: BrowserWaitArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.wait(
                condition=args.condition,
                selector=args.selector,
                timeout=args.timeout,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {"condition": args.condition, "selector": args.selector, **info}
        return ToolResult.ok(f"Wait condition {args.condition} satisfied.", **meta)


class BrowserSelectArgs(_BrowserPageTargetArgs):
    selector: str = Field(..., description="CSS selector for the <select> element.")
    label: str | None = Field(None, description="Select by visible label.")
    value: str | None = Field(None, description="Select by value attribute.")
    index: int | None = Field(None, description="Select by zero-based index.")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )

    @field_validator("label", "value", mode="before")
    @classmethod
    def _normalize_blank_choice(cls, choice: object) -> object:
        if isinstance(choice, str) and not choice.strip():
            return None
        return choice

    @model_validator(mode="after")
    def _require_one_choice(self) -> "BrowserSelectArgs":
        choices = [self.label is not None, self.value is not None, self.index is not None]
        if sum(choices) != 1:
            raise ValueError("provide exactly one of label, value, or index")
        return self


@register_tool
class BrowserSelectTool(BaseTool):
    name: ClassVar[str] = "browser_select"
    description: ClassVar[str] = "Select option(s) from a <select> dropdown."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserSelectArgs

    async def run(self, args: BrowserSelectArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            selected = await backend.select(
                args.selector,
                label=args.label,
                value=args.value,
                index=args.index,
                page_id=args.page_id,
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta: dict[str, Any] = {
            "selector": args.selector,
            "selected": selected,
            **_page_metadata(backend, args.page_id),
        }
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Selected: {selected}.{note}", **meta)


class BrowserKeyboardArgs(_BrowserPageTargetArgs):
    key: str = Field(..., min_length=1, description="Key or combo, e.g. Enter, Escape, Control+A.")
    selector: str | None = Field(None, description="Element to focus before pressing.")
    screenshot: BrowserScreenshotPolicy = Field(
        "auto",
        description="Visual feedback: auto, always, or never; semantic feedback is always returned.",
    )


@register_tool
class BrowserKeyboardTool(BaseTool):
    name: ClassVar[str] = "browser_keyboard"
    description: ClassVar[str] = "Press keyboard keys/shortcuts in the browser."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserKeyboardArgs

    async def run(self, args: BrowserKeyboardArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            info = await backend.keyboard(
                args.key, selector=args.selector, page_id=args.page_id
            )
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        meta = {"key": args.key, "selector": args.selector, **info}
        shot_meta, note = await _capture_post_action_feedback(
            backend, ctx, page_id=args.page_id, screenshot=args.screenshot
        )
        meta.update(shot_meta)
        return ToolResult.ok(f"Pressed {args.key}.{note}", **meta)


class BrowserEvaluateArgs(_BrowserPageTargetArgs):
    script: str = Field(
        ...,
        min_length=1,
        description=(
            "JavaScript expression or function body to evaluate in the page context. "
            "For asynchronous work, use a top-level `return await ...` function body "
            "(for example, `return await fetch(url).then(r => r.json())`). The final "
            "value must be JSON-serializable; do not return a Promise, function, DOM "
            "node, or other host object."
        ),
    )


@register_tool
class BrowserEvaluateTool(BaseTool):
    name: ClassVar[str] = "browser_evaluate"
    description: ClassVar[str] = (
        "Execute JavaScript in the page and return a JSON result; use top-level "
        "`return await ...` for asynchronous work."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserEvaluateArgs

    async def run(self, args: BrowserEvaluateArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            value = await backend.evaluate(args.script, page_id=args.page_id)
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        try:
            result_json = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return ToolResult.error(
                "JavaScript returned a value that is not JSON-serializable. "
                "Return only null, booleans, numbers, strings, arrays, or objects; "
                "for asynchronous work use a top-level `return await ...`.",
                error_kind="unsupported_result",
            )
        return ToolResult.ok(
            result_json,
            value=value,
            result_repr=result_json,
            **_page_metadata(backend, args.page_id),
        )


class BrowserCloseArgs(BaseModel):
    pass


@register_tool
class BrowserCloseTool(BaseTool):
    name: ClassVar[str] = "browser_close"
    description: ClassVar[str] = (
        "Hide the native browser panel or release a headless browser session."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = BrowserCloseArgs

    async def run(self, args: BrowserCloseArgs, ctx: ToolContext) -> ToolResult:
        backend = _backend(ctx)
        if backend is None:
            return ToolResult.error("No browser backend configured.", error_kind="no_backend")
        try:
            await backend.close()
        except BrowserBackendError as exc:
            return ToolResult.error(str(exc), error_kind=exc.error_kind, **exc.metadata)
        return ToolResult.ok("Browser closed.")


__all__ = [
    "BrowserBackend",
    "BrowserBackendError",
    "clamp_screenshot",
    "BrowserListPagesArgs",
    "BrowserListPagesTool",
    "BrowserNewPageArgs",
    "BrowserNewPageTool",
    "BrowserSwitchPageArgs",
    "BrowserSwitchPageTool",
    "BrowserClosePageArgs",
    "BrowserClosePageTool",
    "BrowserNavigateTool",
    "BrowserScreenshotTool",
    "BrowserClickTool",
    "BrowserTypeTool",
    "BrowserGetContentTool",
    "BrowserScrollTool",
    "BrowserWaitTool",
    "BrowserSelectTool",
    "BrowserKeyboardTool",
    "BrowserEvaluateTool",
    "BrowserCloseTool",
    "_JPEG_QUALITY_LADDER",
    "_MAX_SCREENSHOT_BYTES",
    "_MAX_IMAGE_DIMENSION",
]
