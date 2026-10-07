"""Unit: clamp_screenshot guard (T12)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from autoverse.core.tools.builtin.browser import (
    _MAX_SCREENSHOT_BYTES,
    BrowserBackend,
    BrowserClickArgs,
    BrowserClickTool,
    BrowserEvaluateArgs,
    BrowserEvaluateTool,
    BrowserNavigateArgs,
    BrowserNavigateTool,
    BrowserScreenshotArgs,
    BrowserScreenshotTool,
    clamp_screenshot,
)
from autoverse.infra.permission.fixed import DenyAllPermissionAdapter
from autoverse.types import BrowserPageObservation
from autoverse.types.tools import ToolResult
from tests.conftest import make_ctx

PIL = pytest.importorskip("PIL")


def _png(
    width: int, height: int, color: tuple[int, int, int] = (10, 20, 30)
) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _ScreenshotBackend(BrowserBackend):
    def __init__(
        self,
        content: str = "",
        *,
        visual_hint: bool = False,
        evaluate_value: Any = None,
    ) -> None:
        self.content = content
        self.visual_hint = visual_hint
        self.evaluate_value = evaluate_value
        self.observation_policies: list[str] = []

    async def launch(self) -> None: ...

    async def close(self) -> None: ...

    async def list_pages(self) -> list[dict[str, Any]]:
        return [{"page_id": "page-test", "active": True}]

    async def new_page(
        self, url: str | None = None, *, wait_until: str = "domcontentloaded"
    ) -> dict[str, Any]:
        return {"page_id": "page-test", "active": True}

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        return {"page_id": page_id, "active": True}

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        return {"closed_page_id": page_id or "page-test"}

    async def navigate(
        self, url: str, *, wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def screenshot(
        self, *, full_page: bool = False, selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        return _png(50, 50)

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        self.observation_policies.append(screenshot)
        include_image = screenshot == "always" or (
            screenshot == "when_visual" and self.visual_hint
        )
        return BrowserPageObservation(
            page_id=page_id or "page-test",
            revision=3,
            url="https://example.com/dashboard",
            title="Dashboard",
            is_loading=False,
            active=True,
            semantic_snapshot=self.content,
            visual_hint=self.visual_hint,
            screenshot=_png(50, 50) if include_image else None,
            screenshot_media_type="image/png" if include_image else None,
        )

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        return self.evaluate_value

    async def click(
        self,
        selector: str | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        clear: bool = True,
        press_key: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def get_content(
        self, *, selector: str | None = None, mode: str = "text",
        page_id: str | None = None,
    ) -> str:
        return self.content

    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: float = 500,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def wait(
        self,
        *,
        condition: str = "visible",
        selector: str | None = None,
        timeout: float = 10000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def select(
        self,
        selector: str,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        page_id: str | None = None,
    ) -> list[str]:
        return []

    async def keyboard(
        self, key: str, *, selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}


class _BrokenObservationBackend(_ScreenshotBackend):
    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        raise RuntimeError("document changed during observation")


class _NavigatingClickBackend(_ScreenshotBackend):
    @property
    def active_page_id(self) -> str:
        return "page-test"

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        self.observation_policies.append(screenshot)
        return BrowserPageObservation(
            page_id="page-test",
            revision=8,
            url="https://example.com/beta",
            title="Latency Beta",
            is_loading=False,
            active=True,
            semantic_snapshot=self.content,
        )

    async def click(
        self,
        selector: str | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "page_id": "page-test",
            "url": "https://example.com/beta",
            "title": "Latency Beta",
            "is_loading": False,
        }


class _ReadImageArgs(BaseModel):
    sources: list[str]
    question: str = ""


class _ReadImageTool:
    name = "read_image"
    Args = _ReadImageArgs

    def __init__(self) -> None:
        self.calls: list[_ReadImageArgs] = []

    async def run(self, args: _ReadImageArgs, ctx: Any) -> ToolResult:
        self.calls.append(args)
        assert args.sources
        assert Path(args.sources[0]).read_bytes()
        return ToolResult.ok("The page shows a login form.", model="vision-test")


class _Tools:
    def __init__(self, read_image: _ReadImageTool) -> None:
        self.read_image = read_image

    def get(self, name: str) -> object | None:
        if name == "read_image":
            return self.read_image
        return None


def test_t12_small_image_within_budget() -> None:
    data, meta = clamp_screenshot(_png(50, 50))
    assert data is not None
    assert len(data) <= _MAX_SCREENSHOT_BYTES
    assert meta["engine"] == "pillow"


def test_t12_large_image_scaled_down() -> None:
    # A large noisy image; should be scaled/compressed under the budget.
    from PIL import Image
    import os

    img = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data, meta = clamp_screenshot(buf.getvalue())
    # Either it fits after scaling, or it is rejected with a reason — never oversize.
    if data is not None:
        assert len(data) <= _MAX_SCREENSHOT_BYTES
    else:
        assert "too large" in meta["reason"]


async def test_browser_screenshot_metadata_is_json_safe(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.app.browser = _ScreenshotBackend()  # type: ignore[attr-defined]

    res = await BrowserScreenshotTool().run(BrowserScreenshotArgs(), ctx)

    assert res.succeeded
    assert "screenshot" not in res.metadata
    assert res.metadata["image_media_type"] == "image/jpeg"
    assert isinstance(res.metadata["images"], list)
    assert isinstance(res.metadata["images"][0], str)
    json.dumps(res.metadata)


async def test_text_model_browser_screenshot_auto_reads_image(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    read_image = _ReadImageTool()
    ctx.app.browser = _ScreenshotBackend()  # type: ignore[attr-defined]
    ctx.app.tools = _Tools(read_image)
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserScreenshotTool().run(BrowserScreenshotArgs(), ctx)

    assert res.succeeded
    assert len(read_image.calls) == 1
    assert "Browser screenshot description via read_image (vision-test)" in res.output
    assert "login form" in res.output
    assert res.metadata["image_description"] == "The page shows a login form."
    assert res.metadata["image_description_auto"] is True


async def test_text_model_post_action_uses_semantic_snapshot_without_vision_call(
    tmp_path,
) -> None:
    ctx = make_ctx(tmp_path)
    read_image = _ReadImageTool()
    ctx.app.browser = _ScreenshotBackend(  # type: ignore[attr-defined]
        "Dashboard ready\nSigned in as Ada"
    )
    ctx.app.tools = _Tools(read_image)
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(url="https://example.com/dashboard"), ctx
    )

    assert res.succeeded
    assert read_image.calls == []
    assert "Browser semantic snapshot" in res.output
    assert "Dashboard ready" in res.output
    assert res.metadata["page_semantic_snapshot"] == (
        "Dashboard ready\nSigned in as Ada"
    )
    assert res.metadata["page_semantic_snapshot_truncated"] is False
    assert res.metadata["page_state"]["revision"] == 3
    assert res.metadata["screenshot_policy"] == "auto"
    assert "images" not in res.metadata
    assert ctx.app.browser.observation_policies == ["never"]  # type: ignore[attr-defined]


async def test_post_action_semantic_snapshot_is_bounded(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.app.browser = _ScreenshotBackend("x" * 5_000)  # type: ignore[attr-defined]

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(url="https://example.com/large"), ctx
    )

    assert len(res.metadata["page_semantic_snapshot"]) == 4_000
    assert res.metadata["page_semantic_snapshot_chars"] == 4_000
    assert res.metadata["page_semantic_snapshot_truncated"] is True


async def test_post_action_output_includes_final_page_state(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.app.browser = _NavigatingClickBackend(  # type: ignore[attr-defined]
        "Latency Beta\nmarker-beta"
    )
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserClickTool().run(
        BrowserClickArgs(selector="#same-tab", page_id="page-test"), ctx
    )

    assert res.succeeded
    assert "Browser final page state" in res.output
    assert '"url":"https://example.com/beta"' in res.output
    assert '"title":"Latency Beta"' in res.output
    assert '"is_loading":false' in res.output
    assert "marker-beta" in res.output
    assert res.metadata["page_state"] == {
        "page_id": "page-test",
        "revision": 8,
        "url": "https://example.com/beta",
        "title": "Latency Beta",
        "is_loading": False,
        "active": True,
    }


async def test_post_action_observation_failure_does_not_fail_successful_action(
    tmp_path,
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.app.browser = _BrokenObservationBackend()  # type: ignore[attr-defined]
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(url="https://example.com/dashboard"), ctx
    )

    assert res.succeeded
    assert "page action succeeded" in res.output
    assert res.metadata["page_observation_error"] == (
        "document changed during observation"
    )


async def test_vision_model_auto_captures_only_visual_pages(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _ScreenshotBackend("[canvas] chart", visual_hint=True)
    ctx.app.browser = backend  # type: ignore[attr-defined]
    ctx.app.extra["model_supports_vision"] = True

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(url="https://example.com/chart"), ctx
    )

    assert res.succeeded
    assert backend.observation_policies == ["when_visual"]
    assert res.metadata["page_visual_hint"] is True
    assert res.metadata["images"]
    assert "Browser semantic snapshot" in res.output


async def test_vision_model_auto_skips_screenshot_for_semantic_page(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _ScreenshotBackend("heading Dashboard", visual_hint=False)
    ctx.app.browser = backend  # type: ignore[attr-defined]
    ctx.app.extra["model_supports_vision"] = True

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(url="https://example.com/dashboard"), ctx
    )

    assert res.succeeded
    assert backend.observation_policies == ["when_visual"]
    assert res.metadata["page_semantic_snapshot"] == "heading Dashboard"
    assert "images" not in res.metadata


async def test_text_model_explicit_action_screenshot_does_not_call_read_image(
    tmp_path,
) -> None:
    ctx = make_ctx(tmp_path)
    read_image = _ReadImageTool()
    backend = _ScreenshotBackend("Dashboard")
    ctx.app.browser = backend  # type: ignore[attr-defined]
    ctx.app.tools = _Tools(read_image)
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserNavigateTool().run(
        BrowserNavigateArgs(
            url="https://example.com/dashboard", screenshot="always"
        ),
        ctx,
    )

    assert res.succeeded
    assert backend.observation_policies == ["always"]
    assert res.metadata["images"]
    assert read_image.calls == []


async def test_vision_model_browser_screenshot_skips_auto_read_image(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    read_image = _ReadImageTool()
    ctx.app.browser = _ScreenshotBackend()  # type: ignore[attr-defined]
    ctx.app.tools = _Tools(read_image)
    ctx.app.extra["model_supports_vision"] = True

    res = await BrowserScreenshotTool().run(BrowserScreenshotArgs(), ctx)

    assert res.succeeded
    assert read_image.calls == []
    assert "image_description" not in res.metadata


async def test_auto_read_image_honors_permission_policy(tmp_path) -> None:
    ctx = make_ctx(tmp_path, permissions=DenyAllPermissionAdapter())
    read_image = _ReadImageTool()
    ctx.app.browser = _ScreenshotBackend()  # type: ignore[attr-defined]
    ctx.app.tools = _Tools(read_image)
    ctx.app.extra["model_supports_vision"] = False

    res = await BrowserScreenshotTool().run(BrowserScreenshotArgs(), ctx)

    assert res.succeeded
    assert read_image.calls == []
    assert "automatic read_image was denied by policy" in res.output
    assert "permission denied" in res.metadata["image_description_error"]


async def test_browser_evaluate_renders_strict_json(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    value = {"name": "Ada", "enabled": True, "items": [1, None]}
    ctx.app.browser = _ScreenshotBackend(evaluate_value=value)  # type: ignore[attr-defined]

    res = await BrowserEvaluateTool().run(
        BrowserEvaluateArgs(script="return {name: 'Ada'}"), ctx
    )

    assert res.succeeded
    assert res.output == '{"name":"Ada","enabled":true,"items":[1,null]}'
    assert json.loads(res.output) == value
    assert res.metadata["value"] == value
    assert res.metadata["result_repr"] == res.output


async def test_browser_evaluate_rejects_non_json_backend_result(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.app.browser = _ScreenshotBackend(  # type: ignore[attr-defined]
        evaluate_value=float("nan")
    )

    res = await BrowserEvaluateTool().run(
        BrowserEvaluateArgs(script="Number.NaN"), ctx
    )

    assert res.is_error
    assert res.error_kind == "unsupported_result"
    assert "return await" in res.output
    assert "value" not in res.metadata
