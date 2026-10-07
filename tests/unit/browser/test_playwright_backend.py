"""Unit tests for the Playwright browser backend and session wrapper."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest

from autoverse.core.tools.builtin.browser import BrowserBackend
from autoverse.infra.browser import BrowserRuntimeConfig, BrowserSession
from autoverse.infra.browser.playwright import PlaywrightBrowserBackend
from autoverse.ports import BrowserError
from autoverse.types import BrowserPageObservation


class _Response:
    status = 204


Call = tuple[Any, ...]


class _Keyboard:
    def __init__(self, calls: list[Call]) -> None:
        self._calls = calls

    async def type(self, text: str) -> None:
        self._calls.append(("keyboard.type", text))

    async def press(self, key: str) -> None:
        self._calls.append(("keyboard.press", key))


class _Locator:
    def __init__(self, calls: list[Call], selector: str) -> None:
        self._calls = calls
        self._selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    async def inner_html(self) -> str:
        self._calls.append(("locator.inner_html", self._selector))
        return "<b>ok</b>"

    async def inner_text(self) -> str:
        self._calls.append(("locator.inner_text", self._selector))
        return "ok text"

    async def evaluate(self, script: str, arg: object | None = None) -> str:
        self._calls.append(("locator.evaluate", self._selector, script, arg))
        return "markdown"

    async def screenshot(self, *, type: str) -> bytes:
        self._calls.append(("locator.screenshot", self._selector, type))
        return b"element-png"

    async def count(self) -> int:
        self._calls.append(("locator.count", self._selector))
        return 1

    async def click(self, *, button: str = "left") -> None:
        self._calls.append(("locator.click", self._selector, button))

    async def dblclick(self, *, button: str = "left") -> None:
        self._calls.append(("locator.dblclick", self._selector, button))

    async def select_option(self, **kwargs: object) -> list[str]:
        self._calls.append(("locator.select_option", self._selector, kwargs))
        return ["v1"]


class _Page:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.url = "about:blank"
        self.keyboard = _Keyboard(self.calls)
        self.mouse = _Mouse(self.calls)

    def set_default_timeout(self, timeout: float) -> None:
        self.calls.append(("set_default_timeout", timeout))

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.calls.append(("set_default_navigation_timeout", timeout))

    async def goto(self, url: str, *, wait_until: str, timeout: float) -> _Response:
        self.url = url
        self.calls.append(("goto", url, wait_until, timeout))
        return _Response()

    async def title(self) -> str:
        return "Title"

    async def screenshot(self, *, full_page: bool, type: str) -> bytes:
        self.calls.append(("screenshot", full_page, type))
        return b"png"

    async def evaluate(self, script: str, arg: object | None = None) -> str:
        self.calls.append(("evaluate", script, arg))
        if "VISUAL_READINESS_TIMEOUT_MS" in script:
            return {  # type: ignore[return-value]
                "timed_out": False,
                "ready_state": "complete",
            }
        if "const LIMIT = 6000" in script:
            return {  # type: ignore[return-value]
                "snapshot": 'document "Title"\n[button] "Run" selector=#run',
                "truncated": False,
                "visual_hint": False,
            }
        if script == "document.readyState":
            return "complete"
        return "eval"

    async def click(self, selector: str, *, button: str = "left") -> None:
        self.calls.append(("click", selector, button))

    async def dblclick(self, selector: str, *, button: str = "left") -> None:
        self.calls.append(("dblclick", selector, button))

    async def fill(self, selector: str, text: str) -> None:
        self.calls.append(("fill", selector, text))

    async def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", selector, key))

    async def content(self) -> str:
        self.calls.append(("content", None))
        return "<html></html>"

    def locator(self, selector: str) -> _Locator:
        return _Locator(self.calls, selector)

    async def wait_for_selector(
        self, selector: str, *, state: str, timeout: float
    ) -> None:
        self.calls.append(("wait_for_selector", selector, state, timeout))

    async def wait_for_load_state(self, state: str, *, timeout: float) -> None:
        self.calls.append(("wait_for_load_state", state, timeout))

    async def focus(self, selector: str) -> None:
        self.calls.append(("focus", selector))

    async def close(self) -> None:
        self.calls.append(("page.close", None))


class _ContinuouslyNavigatingPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.main_frame = object()
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def evaluate(self, script: str, arg: object | None = None) -> str:
        if "const LIMIT = 6000" in script:
            self.handlers["framenavigated"](self.main_frame)
        return await super().evaluate(script, arg)


class _NavigatingDuringScreenshotPage(_ContinuouslyNavigatingPage):
    async def evaluate(self, script: str, arg: object | None = None) -> str:
        if "VISUAL_READINESS_TIMEOUT_MS" in script:
            self.handlers["framenavigated"](self.main_frame)
        return await _Page.evaluate(self, script, arg)


class _BrokenReadinessPage(_Page):
    async def evaluate(self, script: str, arg: object | None = None) -> str:
        if "VISUAL_READINESS_TIMEOUT_MS" in script:
            raise RuntimeError("readiness script unavailable")
        return await super().evaluate(script, arg)


class _HangingEvaluatePage(_Page):
    async def evaluate(self, script: str, arg: object | None = None) -> str:
        await asyncio.Event().wait()
        return "unreachable"


class _SettlingPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.visually_ready = False

    async def evaluate(self, script: str, arg: object | None = None) -> str:
        if "VISUAL_READINESS_TIMEOUT_MS" in script:
            self.visually_ready = True
        if "const LIMIT = 6000" in script:
            self.calls.append(("evaluate", script, arg))
            marker = "after-paint" if self.visually_ready else "before-paint"
            return {  # type: ignore[return-value]
                "snapshot": marker,
                "truncated": False,
                "visual_hint": True,
            }
        return await super().evaluate(script, arg)


class _Mouse:
    def __init__(self, calls: list[Call]) -> None:
        self._calls = calls

    async def click(self, x: float, y: float, *, button: str = "left") -> None:
        self._calls.append(("mouse.click", x, y, button))

    async def dblclick(self, x: float, y: float, *, button: str = "left") -> None:
        self._calls.append(("mouse.dblclick", x, y, button))


class _Context:
    def __init__(self, page: _Page, calls: list[Call]) -> None:
        self._page = page
        self._calls = calls
        self._created = 0

    async def new_page(self) -> _Page:
        self._calls.append(("new_page", None))
        self._created += 1
        return self._page if self._created == 1 else _Page()

    async def close(self) -> None:
        self._calls.append(("context.close", None))


class _Browser:
    def __init__(self, page: _Page, calls: list[Call]) -> None:
        self._page = page
        self._calls = calls

    async def new_context(self, *, viewport: dict[str, int]) -> _Context:
        self._calls.append(("new_context", viewport))
        return _Context(self._page, self._calls)

    async def close(self) -> None:
        self._calls.append(("browser.close", None))


class _Chromium:
    def __init__(self, page: _Page, calls: list[Call]) -> None:
        self._page = page
        self._calls = calls

    async def launch(self, *, headless: bool) -> _Browser:
        self._calls.append(("chromium.launch", headless))
        return _Browser(self._page, self._calls)


class _Playwright:
    def __init__(self, page: _Page, calls: list[Call]) -> None:
        self.chromium = _Chromium(page, calls)


class _Manager:
    def __init__(self, page: _Page, calls: list[Call]) -> None:
        self._page = page
        self._calls = calls

    async def start(self) -> _Playwright:
        self._calls.append(("manager.start", None))
        return _Playwright(self._page, self._calls)

    async def stop(self) -> None:
        self._calls.append(("manager.stop", None))


async def test_playwright_backend_delegates_page_operations() -> None:
    page = _Page()
    lifecycle_calls: list[Call] = []
    backend = PlaywrightBrowserBackend(
        BrowserRuntimeConfig(timeout_ms=123, viewport_width=800, viewport_height=600),
        async_playwright_factory=lambda: _Manager(page, lifecycle_calls),
    )

    nav = await backend.navigate("https://example.com", wait_until="load")
    assert nav["url"] == "https://example.com"
    assert nav["title"] == "Title"
    assert nav["status"] == 204
    assert nav["active"] is True
    assert nav["page_id"] == backend.active_page_id
    observation = await backend.observe_page(screenshot="never")
    assert observation.revision >= 1
    assert observation.semantic_snapshot.endswith('selector=#run')
    assert observation.screenshot is None
    visual = await backend.observe_page(screenshot="always")
    assert visual.screenshot == b"png"
    assert visual.screenshot_media_type == "image/png"
    assert await backend.screenshot(full_page=True) == b"png"
    assert await backend.screenshot(selector="#main") == b"element-png"
    assert await backend.evaluate("return 2") == "eval"
    assert await backend.get_content(selector="#main", mode="html") == "<b>ok</b>"
    assert await backend.get_content(selector="#main", mode="markdown") == "markdown"
    assert await backend.get_content(mode="text") == "ok text"
    await backend.click("#btn", button="right", double=True)
    await backend.click(x=10, y=20)
    await backend.type_text("#q", "hello", press_key="Enter")
    await backend.scroll(direction="up", amount=10)
    await backend.wait(condition="visible", selector="#q", timeout=50)
    assert await backend.select("#s", value="v1") == ["v1"]
    await backend.keyboard("Escape", selector="#q")
    await backend.close()

    assert ("chromium.launch", True) in lifecycle_calls
    assert ("new_context", {"width": 800, "height": 600}) in lifecycle_calls
    assert ("goto", "https://example.com", "load", 123) in page.calls
    assert ("screenshot", True, "png") in page.calls
    assert ("locator.screenshot", "#main", "png") in page.calls
    assert ("locator.dblclick", "#btn", "right") in page.calls
    assert ("mouse.click", 10, 20, "left") in page.calls
    assert ("fill", "#q", "hello") in page.calls
    assert ("press", "#q", "Enter") in page.calls
    assert ("wait_for_selector", "#q", "visible", 50) in page.calls
    assert ("locator.select_option", "#s", {"value": "v1"}) in page.calls
    assert ("focus", "#q") in page.calls
    assert ("keyboard.press", "Escape") in page.calls
    readiness_positions = [
        index
        for index, call in enumerate(page.calls)
        if call[0] == "evaluate" and "VISUAL_READINESS_TIMEOUT_MS" in call[1]
    ]
    capture_positions = [
        index
        for index, call in enumerate(page.calls)
        if call[0] in {"screenshot", "locator.screenshot"}
    ]
    assert len(readiness_positions) == 3
    assert len(capture_positions) == 3
    assert all(
        readiness < capture
        for readiness, capture in zip(readiness_positions, capture_positions)
    )


async def test_playwright_evaluate_has_a_bounded_timeout() -> None:
    page = _HangingEvaluatePage()
    lifecycle_calls: list[Call] = []
    backend = PlaywrightBrowserBackend(
        BrowserRuntimeConfig(timeout_ms=10),
        async_playwright_factory=lambda: _Manager(page, lifecycle_calls),
    )

    with pytest.raises(BrowserError) as raised:
        await backend.evaluate("return await new Promise(() => {})")

    assert raised.value.error_kind == "browser_timeout"
    assert str(raised.value) == "JavaScript evaluation timed out."
    await backend.close()


async def test_playwright_backend_manages_stable_page_ids() -> None:
    first_page = _Page()
    lifecycle_calls: list[Call] = []
    backend = PlaywrightBrowserBackend(
        async_playwright_factory=lambda: _Manager(first_page, lifecycle_calls)
    )

    await backend.navigate("https://first.example")
    first = (await backend.list_pages())[0]
    second = await backend.new_page("https://second.example", wait_until="load")

    assert second["page_id"] != first["page_id"]
    assert second["active"] is True
    assert backend.active_page_id == second["page_id"]
    switched = await backend.switch_page(first["page_id"])
    assert switched["page_id"] == first["page_id"]
    assert backend.active_page_id == first["page_id"]

    targeted = await backend.navigate(
        "https://second-updated.example", page_id=second["page_id"]
    )
    assert targeted["page_id"] == second["page_id"]
    assert backend.active_page_id == second["page_id"]

    closed = await backend.close_page(second["page_id"])
    assert closed["closed_page_id"] == second["page_id"]
    assert closed["active_page_id"] == first["page_id"]
    assert [item["page_id"] for item in await backend.list_pages()] == [
        first["page_id"]
    ]

    last = await backend.close_page(first["page_id"])
    assert last["page_count"] == 0
    assert await backend.list_pages() == []
    reopened = await backend.navigate("https://fresh.example")
    assert reopened["page_id"] != first["page_id"]
    assert len(await backend.list_pages()) == 1


async def test_playwright_observation_rejects_cross_revision_feedback() -> None:
    page = _ContinuouslyNavigatingPage()
    backend = PlaywrightBrowserBackend(
        async_playwright_factory=lambda: _Manager(page, [])
    )
    await backend.navigate("https://example.com")

    with pytest.raises(BrowserError) as caught:
        await backend.observe_page(screenshot="always")

    assert caught.value.error_kind == "browser_state_unstable"


async def test_playwright_screenshot_rejects_cross_revision_capture() -> None:
    page = _NavigatingDuringScreenshotPage()
    backend = PlaywrightBrowserBackend(
        async_playwright_factory=lambda: _Manager(page, [])
    )
    await backend.navigate("https://example.com")

    with pytest.raises(BrowserError) as caught:
        await backend.screenshot()

    assert caught.value.error_kind == "browser_state_unstable"


async def test_playwright_screenshot_readiness_is_best_effort() -> None:
    page = _BrokenReadinessPage()
    backend = PlaywrightBrowserBackend(
        async_playwright_factory=lambda: _Manager(page, [])
    )
    await backend.navigate("https://example.com")

    assert await backend.screenshot() == b"png"


async def test_playwright_observation_refreshes_semantics_before_capture() -> None:
    page = _SettlingPage()
    backend = PlaywrightBrowserBackend(
        async_playwright_factory=lambda: _Manager(page, [])
    )
    await backend.navigate("https://example.com")

    observation = await backend.observe_page(screenshot="when_visual")

    assert observation.semantic_snapshot == "after-paint"
    assert observation.screenshot == b"png"


class _SlowBackend(BrowserBackend):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def _record(self, name: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.calls.append(name)
        self.active -= 1

    async def launch(self) -> None:
        return None

    async def close(self) -> None:
        self.calls.append("close")

    async def list_pages(self) -> list[dict[str, Any]]:
        return []

    async def new_page(
        self,
        url: str | None = None,
        *,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        return {}

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        return {}

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        return {}

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        await self._record(f"navigate:{url}")
        return {}

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        await self._record("screenshot")
        return b"png"

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        await self._record("observe")
        return BrowserPageObservation(
            page_id=page_id or "page-test",
            revision=1,
            url="about:blank",
            title="Test",
            is_loading=False,
            active=True,
            semantic_snapshot='document "Test"',
        )

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        await self._record(f"evaluate:{script}")
        return None

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
        self,
        *,
        selector: str | None = None,
        mode: str = "text",
        page_id: str | None = None,
    ) -> str:
        return ""

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
        timeout: float = 10_000,
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
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}


async def test_browser_session_serializes_shared_page_operations() -> None:
    backend = _SlowBackend()
    session = BrowserSession(backend_factory=lambda: backend)

    await asyncio.gather(
        session.navigate("https://a.example"),
        session.evaluate("document.title"),
        session.screenshot(),
    )

    assert backend.max_active == 1
    assert sorted(backend.calls) == [
        "evaluate:document.title",
        "navigate:https://a.example",
        "screenshot",
    ]
