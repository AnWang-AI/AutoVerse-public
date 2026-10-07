"""Playwright browser backend and session lifecycle (docs/reference/tools.md §12).

The backend is intentionally optional: importing this module does not import
Playwright. A session lazily starts the driver on first browser tool use, owns
an ordered page set, and serializes page operations.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from autoverse.ports import BrowserError as BrowserBackendError
from autoverse.ports import BrowserPort as BrowserBackend
from autoverse.types import BrowserPageObservation

_T = TypeVar("_T")


@dataclass(frozen=True)
class BrowserRuntimeConfig:
    """Runtime browser settings projected from the app config tree."""

    backend: str = "auto"
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 900
    timeout_ms: float = 10_000


def playwright_available() -> bool:
    """Whether the optional Playwright Python package is importable."""

    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except ModuleNotFoundError:
        return False


class PlaywrightBrowserBackend(BrowserBackend):
    """Concrete ``BrowserBackend`` using Playwright's async Chromium driver."""

    def __init__(
        self,
        config: BrowserRuntimeConfig | None = None,
        *,
        async_playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config or BrowserRuntimeConfig()
        self._factory = async_playwright_factory
        self._manager: Any | None = None
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._pages: dict[str, Any] = {}
        self._page_order: list[str] = []
        self._active_page_id: str | None = None
        self._page_revisions: dict[str, int] = {}

    @property
    def active_page_id(self) -> str | None:
        """Stable id of the page targeted by unqualified operations."""

        return self._active_page_id

    async def launch(self) -> None:
        """Start Playwright, Chromium, a context, and one shared page lazily."""

        if self._context is not None:
            return
        try:
            factory = self._factory
            if factory is None:
                from playwright.async_api import async_playwright

                factory = async_playwright
            self._manager = factory()
            self._playwright = await self._manager.start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._config.headless
            )
            self._context = await self._browser.new_context(
                viewport={
                    "width": self._config.viewport_width,
                    "height": self._config.viewport_height,
                }
            )
            on = getattr(self._context, "on", None)
            if callable(on):
                on("page", lambda page: self._register_page(page, activate=True))
            await self._create_page()
        except ModuleNotFoundError as exc:
            await self.close()
            raise BrowserBackendError(
                "Playwright is not installed. Install with: pip install -e '.[browser]'",
                error_kind="browser_unavailable",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize driver startup errors
            await self.close()
            raise BrowserBackendError(
                "Browser backend failed to start. If Playwright is installed, run: "
                "python -m playwright install chromium",
                error_kind="browser_unavailable",
                metadata={"detail": str(exc)},
            ) from exc

    async def close(self) -> None:
        """Release page, context, browser, and Playwright manager best-effort."""

        context, browser = self._context, self._browser
        manager = self._manager
        self._page = self._context = self._browser = None
        self._playwright = self._manager = None
        self._pages.clear()
        self._page_order.clear()
        self._page_revisions.clear()
        self._active_page_id = None
        for obj in (context, browser):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001 - shutdown must be best-effort
                    pass
        if manager is not None:
            stop = getattr(manager, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:  # noqa: BLE001 - shutdown must be best-effort
                    pass

    async def list_pages(self) -> list[dict[str, Any]]:
        """Return ordered page metadata without changing the active page."""

        await self.launch()
        pages: list[dict[str, Any]] = []
        for page_id in tuple(self._page_order):
            page = self._pages.get(page_id)
            if page is None:
                continue
            pages.append(await self._page_state(page_id, page))
        return pages

    async def new_page(
        self,
        url: str | None = None,
        *,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        """Create and activate a page, optionally navigating it immediately."""

        async def op() -> dict[str, Any]:
            await self.launch()
            page_id, page = await self._create_page()
            status: int | None = None
            if url is not None:
                response = await page.goto(
                    url, wait_until=wait_until, timeout=self._config.timeout_ms
                )
                self._increment_page_revision(page_id)
                status = getattr(response, "status", None)
            return await self._page_state(page_id, page, status=status)

        return await self._guarded(op)

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        """Activate an existing page by stable id."""

        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            return await self._page_state(page_id, page)

        return await self._guarded(op)

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        """Close one page and select an adjacent survivor, if any."""

        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            closed_id = self._active_page_id
            assert closed_id is not None
            closed_index = self._page_order.index(closed_id)
            await page.close()
            self._unregister_page(closed_id)
            if self._page_order:
                next_index = min(closed_index, len(self._page_order) - 1)
                next_id = self._page_order[next_index]
                self._activate_page(next_id)
            return {
                "closed_page_id": closed_id,
                "active_page_id": self._active_page_id,
                "page_count": len(self._page_order),
            }

        return await self._guarded(op)

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            response = await page.goto(
                url, wait_until=wait_until, timeout=self._config.timeout_ms
            )
            assert self._active_page_id is not None
            self._increment_page_revision(self._active_page_id)
            return await self._page_state(
                self._active_page_id,
                page,
                status=getattr(response, "status", None),
            )

        return await self._guarded(op)

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        """Capture one revision-checked semantic observation of a page."""

        async def op() -> BrowserPageObservation:
            page = await self._ensure_page(page_id)
            assert self._active_page_id is not None
            target_id = self._active_page_id
            for _ in range(2):
                revision = self._page_revisions.get(target_id, 0)
                raw = await page.evaluate(_JS_PAGE_OBSERVATION)
                semantic = raw if isinstance(raw, dict) else {}
                visual_hint = bool(semantic.get("visual_hint", False))
                should_capture = screenshot == "always" or (
                    screenshot == "when_visual" and visual_hint
                )
                image: bytes | None = None
                image_error: str | None = None
                if should_capture:
                    try:
                        await _wait_for_visual_readiness(page)
                        refreshed = await page.evaluate(_JS_PAGE_OBSERVATION)
                        if isinstance(refreshed, dict):
                            semantic = refreshed
                            visual_hint = bool(
                                semantic.get("visual_hint", False)
                            )
                        should_capture = screenshot == "always" or visual_hint
                        if should_capture:
                            captured = await page.screenshot(
                                full_page=False, type="png"
                            )
                            if isinstance(captured, bytes):
                                image = captured
                            else:
                                image_error = (
                                    "Browser screenshot did not return bytes."
                                )
                    except Exception as exc:  # noqa: BLE001 - observation is best-effort
                        image_error = str(exc)
                state = await self._page_state(target_id, page)
                observation = BrowserPageObservation(
                    page_id=target_id,
                    revision=int(state.get("revision") or 0),
                    url=str(state.get("url") or ""),
                    title=str(state.get("title") or ""),
                    is_loading=bool(state.get("is_loading", False)),
                    active=bool(state.get("active", False)),
                    semantic_snapshot=str(semantic.get("snapshot") or ""),
                    semantic_truncated=bool(semantic.get("truncated", False)),
                    visual_hint=visual_hint,
                    screenshot=image,
                    screenshot_media_type="image/png" if image is not None else None,
                    screenshot_error=image_error,
                )
                if self._page_revisions.get(target_id, 0) == revision:
                    return observation
            raise BrowserBackendError(
                "Browser page changed repeatedly during observation.",
                error_kind="browser_state_unstable",
                metadata={"page_id": target_id},
            )

        return await self._guarded(op)

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        async def op() -> bytes:
            page = await self._ensure_page(page_id)
            assert self._active_page_id is not None
            target_id = self._active_page_id
            for _ in range(2):
                revision = self._page_revisions.get(target_id, 0)
                await _wait_for_visual_readiness(page)
                if selector:
                    data = await page.locator(selector).first.screenshot(type="png")
                else:
                    data = await page.screenshot(full_page=full_page, type="png")
                if not isinstance(data, bytes):
                    raise BrowserBackendError(
                        "Browser screenshot did not return bytes.",
                        error_kind="browser_error",
                    )
                if self._page_revisions.get(target_id, 0) == revision:
                    return data
            raise BrowserBackendError(
                "Browser page changed repeatedly during screenshot capture.",
                error_kind="browser_state_unstable",
                metadata={"page_id": target_id},
            )

        return await self._guarded(op)

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        async def op() -> Any:
            page = await self._ensure_page(page_id)
            prepared = _prepare_evaluate_script(script)
            try:
                return await asyncio.wait_for(
                    page.evaluate(prepared),
                    timeout=self._config.timeout_ms / 1000,
                )
            except TimeoutError as exc:
                raise BrowserBackendError(
                    "JavaScript evaluation timed out.",
                    error_kind="browser_timeout",
                ) from exc

        return await self._guarded(op)

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
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            if selector:
                loc = page.locator(selector)
                count = await loc.count()
                if count == 0:
                    raise BrowserBackendError(
                        f"No element matched selector {selector!r}.",
                        error_kind="not_found",
                        metadata={"selector": selector},
                    )
                if count > 1:
                    raise BrowserBackendError(
                        f"Selector {selector!r} matched {count} elements; "
                        "use a more specific selector.",
                        error_kind="ambiguous_selector",
                        metadata={"selector": selector, "count": count},
                    )
                target = loc.first
                if double:
                    await target.dblclick(button=button)
                else:
                    await target.click(button=button)
                return {
                    "button": button,
                    "mode": "selector",
                    "page_id": self._active_page_id,
                }
            if x is not None and y is not None:
                if double:
                    await page.mouse.dblclick(x, y, button=button)
                else:
                    await page.mouse.click(x, y, button=button)
                return {
                    "button": button,
                    "mode": "coordinate",
                    "page_id": self._active_page_id,
                }
            else:
                raise BrowserBackendError(
                    "Provide either selector or both x/y coordinates.",
                    error_kind="invalid_args",
                )

        return await self._guarded(op)

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        clear: bool = True,
        press_key: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            if clear:
                await page.fill(selector, text)
            else:
                await page.click(selector)
                await page.keyboard.type(text)
            if press_key:
                await page.press(selector, press_key)
            return {
                "chars": len(text),
                "clear": clear,
                "press_key": press_key,
                "page_id": self._active_page_id,
            }

        return await self._guarded(op)

    async def get_content(
        self,
        *,
        selector: str | None = None,
        mode: str = "text",
        page_id: str | None = None,
    ) -> str:
        async def op() -> str:
            page = await self._ensure_page(page_id)
            target = page.locator(selector).first if selector else None
            normalized = mode.lower()
            if normalized == "html":
                if target is not None:
                    return str(await target.inner_html())
                return str(await page.content())
            if normalized == "markdown":
                if target is not None:
                    return str(await target.evaluate(_JS_TO_MARKDOWN_ELEMENT))
                return str(await page.evaluate(_JS_TO_MARKDOWN))
            if normalized == "text":
                if target is not None:
                    return str(await target.inner_text())
                return str(await page.locator("body").inner_text())
            raise BrowserBackendError(
                "Unsupported browser_get_content mode. Use text, html, or markdown.",
                error_kind="invalid_args",
                metadata={"mode": mode},
            )

        return await self._guarded(op)

    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: float = 500,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            dx, dy = _scroll_delta(direction, amount)
            if selector:
                await page.locator(selector).evaluate(
                    "(el, delta) => el.scrollBy(delta.dx, delta.dy)",
                    {"dx": dx, "dy": dy},
                )
            else:
                await page.evaluate(
                    "(delta) => window.scrollBy(delta.dx, delta.dy)",
                    {"dx": dx, "dy": dy},
                )
            return {"dx": dx, "dy": dy, "page_id": self._active_page_id}

        return await self._guarded(op)

    async def wait(
        self,
        *,
        condition: str = "visible",
        selector: str | None = None,
        timeout: float = 10_000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            normalized = condition.lower()
            if normalized in {"visible", "hidden", "attached", "detached"}:
                if not selector:
                    raise BrowserBackendError(
                        f"browser_wait condition {condition!r} requires a selector.",
                        error_kind="invalid_args",
                        metadata={"condition": condition},
                    )
                await page.wait_for_selector(selector, state=normalized, timeout=timeout)
            elif normalized in {"load", "domcontentloaded", "networkidle"}:
                await page.wait_for_load_state(normalized, timeout=timeout)
            else:
                raise BrowserBackendError(
                    "Unsupported browser_wait condition.",
                    error_kind="invalid_args",
                    metadata={"condition": condition},
                )
            return {"timeout": timeout, "page_id": self._active_page_id}

        return await self._guarded(op)

    async def select(
        self,
        selector: str,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        page_id: str | None = None,
    ) -> list[str]:
        async def op() -> list[str]:
            page = await self._ensure_page(page_id)
            choices = [label is not None, value is not None, index is not None]
            if sum(choices) != 1:
                raise BrowserBackendError(
                    "browser_select requires exactly one of label, value, or index.",
                    error_kind="invalid_args",
                )
            option: dict[str, str | int]
            if label is not None:
                option = {"label": label}
            elif value is not None:
                option = {"value": value}
            else:
                assert index is not None
                option = {"index": int(index)}
            selected = await page.locator(selector).first.select_option(**option)
            return [str(item) for item in selected]

        return await self._guarded(op)

    async def keyboard(
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            page = await self._ensure_page(page_id)
            if selector:
                await page.focus(selector)
            await page.keyboard.press(key)
            return {
                "focused": selector is not None,
                "page_id": self._active_page_id,
            }

        return await self._guarded(op)

    async def _ensure_page(self, page_id: str | None = None) -> Any:
        await self.launch()
        target_id = page_id or self._active_page_id
        if target_id is None and page_id is None:
            _, page = await self._create_page()
            return page
        if target_id is None or target_id not in self._pages:
            if page_id is not None:
                raise BrowserBackendError(
                    f"Browser page {page_id!r} was not found.",
                    error_kind="page_not_found",
                    metadata={"page_id": page_id},
                )
            raise BrowserBackendError(
                "Browser page is unavailable after launch.",
                error_kind="browser_unavailable",
            )
        self._activate_page(target_id)
        assert self._page is not None
        return self._page

    async def _create_page(self) -> tuple[str, Any]:
        if self._context is None:
            raise BrowserBackendError(
                "Browser context is unavailable after launch.",
                error_kind="browser_unavailable",
            )
        page = await self._context.new_page()
        page_id = self._register_page(page, activate=True)
        return page_id, page

    def _register_page(self, page: Any, *, activate: bool) -> str:
        for existing_id, existing_page in self._pages.items():
            if existing_page is page:
                if activate:
                    self._activate_page(existing_id)
                return existing_id
        page_id = f"page-{uuid.uuid4().hex[:12]}"
        self._pages[page_id] = page
        self._page_order.append(page_id)
        self._page_revisions[page_id] = 0
        page.set_default_timeout(self._config.timeout_ms)
        page.set_default_navigation_timeout(self._config.timeout_ms)
        on = getattr(page, "on", None)
        if callable(on):
            on("close", lambda *_: self._unregister_page(page_id))
            on(
                "framenavigated",
                lambda frame: self._record_frame_navigation(page_id, page, frame),
            )
        if activate:
            self._activate_page(page_id)
        return page_id

    def _activate_page(self, page_id: str) -> None:
        page = self._pages.get(page_id)
        if page is None:
            raise BrowserBackendError(
                f"Browser page {page_id!r} was not found.",
                error_kind="page_not_found",
                metadata={"page_id": page_id},
            )
        self._active_page_id = page_id
        self._page = page

    def _unregister_page(self, page_id: str) -> None:
        self._pages.pop(page_id, None)
        self._page_revisions.pop(page_id, None)
        if page_id in self._page_order:
            self._page_order.remove(page_id)
        if self._active_page_id == page_id:
            self._active_page_id = None
            self._page = None

    async def _page_state(
        self,
        page_id: str,
        page: Any,
        *,
        status: int | None = None,
    ) -> dict[str, Any]:
        try:
            ready_state = str(await page.evaluate("document.readyState"))
        except Exception:  # noqa: BLE001 - metadata must not break page listing
            ready_state = "complete"
        return {
            "page_id": page_id,
            "revision": self._page_revisions.get(page_id, 0),
            "url": str(page.url),
            "title": str(await page.title()),
            "is_loading": ready_state != "complete",
            "active": page_id == self._active_page_id,
            "status": status,
        }

    def _increment_page_revision(self, page_id: str) -> None:
        self._page_revisions[page_id] = self._page_revisions.get(page_id, 0) + 1

    def _record_frame_navigation(self, page_id: str, page: Any, frame: Any) -> None:
        main_frame = getattr(page, "main_frame", None)
        if main_frame is None or frame is main_frame:
            self._increment_page_revision(page_id)

    @staticmethod
    async def _guarded(op: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await op()
        except BrowserBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize Playwright errors
            raise BrowserBackendError(
                f"Browser operation failed: {exc}",
                error_kind="browser_error",
                metadata={"detail": str(exc)},
            ) from exc


class BrowserSession(BrowserBackend):
    """Session-scoped lazy browser wrapper with serialized page operations."""

    def __init__(
        self,
        config: BrowserRuntimeConfig | None = None,
        *,
        backend_factory: Callable[[], BrowserBackend] | None = None,
    ) -> None:
        self._config = config or BrowserRuntimeConfig()
        self._backend_factory = backend_factory
        self._backend: BrowserBackend | None = None
        self._launch_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()

    @property
    def active_page_id(self) -> str | None:
        return getattr(self._backend, "active_page_id", None)

    async def launch(self) -> None:
        async with self._launch_lock:
            if self._backend is None:
                factory = self._backend_factory
                self._backend = (
                    factory()
                    if factory is not None
                    else PlaywrightBrowserBackend(self._config)
                )
            await self._backend.launch()

    async def close(self) -> None:
        async with self._op_lock:
            backend = self._backend
            self._backend = None
            if backend is not None:
                await backend.close()

    async def list_pages(self) -> list[dict[str, Any]]:
        async with self._op_lock:
            await self.launch()
            return await self._required().list_pages()

    async def new_page(
        self,
        url: str | None = None,
        *,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().new_page(url, wait_until=wait_until)

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().switch_page(page_id)

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().close_page(page_id)

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().navigate(
                url, wait_until=wait_until, page_id=page_id
            )

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        async with self._op_lock:
            await self.launch()
            return await self._required().screenshot(
                full_page=full_page, selector=selector, page_id=page_id
            )

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        async with self._op_lock:
            await self.launch()
            return await self._required().observe_page(
                page_id=page_id, screenshot=screenshot
            )

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        async with self._op_lock:
            await self.launch()
            return await self._required().evaluate(script, page_id=page_id)

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
        async with self._op_lock:
            await self.launch()
            return await self._required().click(
                selector,
                x=x,
                y=y,
                button=button,
                double=double,
                page_id=page_id,
            )

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        clear: bool = True,
        press_key: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().type_text(
                selector,
                text,
                clear=clear,
                press_key=press_key,
                page_id=page_id,
            )

    async def get_content(
        self,
        *,
        selector: str | None = None,
        mode: str = "text",
        page_id: str | None = None,
    ) -> str:
        async with self._op_lock:
            await self.launch()
            return await self._required().get_content(
                selector=selector, mode=mode, page_id=page_id
            )

    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: float = 500,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().scroll(
                direction=direction,
                amount=amount,
                selector=selector,
                page_id=page_id,
            )

    async def wait(
        self,
        *,
        condition: str = "visible",
        selector: str | None = None,
        timeout: float = 10_000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().wait(
                condition=condition,
                selector=selector,
                timeout=timeout,
                page_id=page_id,
            )

    async def select(
        self,
        selector: str,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        page_id: str | None = None,
    ) -> list[str]:
        async with self._op_lock:
            await self.launch()
            return await self._required().select(
                selector,
                label=label,
                value=value,
                index=index,
                page_id=page_id,
            )

    async def keyboard(
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._op_lock:
            await self.launch()
            return await self._required().keyboard(
                key, selector=selector, page_id=page_id
            )

    def _required(self) -> BrowserBackend:
        if self._backend is None:
            raise BrowserBackendError(
                "Browser backend is not initialized.",
                error_kind="browser_unavailable",
            )
        return self._backend


def _scroll_delta(direction: str, amount: float) -> tuple[float, float]:
    normalized = direction.lower()
    if normalized == "down":
        return 0, amount
    if normalized == "up":
        return 0, -amount
    if normalized == "right":
        return amount, 0
    if normalized == "left":
        return -amount, 0
    raise BrowserBackendError(
        "Unsupported browser_scroll direction. Use down, up, left, or right.",
        error_kind="invalid_args",
        metadata={"direction": direction},
    )


def _prepare_evaluate_script(script: str) -> str:
    stripped = script.strip()
    if re.search(r"\breturn\b", stripped) and not stripped.startswith("("):
        return f"(async () => {{ {stripped} }})()"
    return script


async def _wait_for_visual_readiness(page: Any) -> None:
    """Best-effort bounded wait for the current viewport to finish painting."""

    try:
        await page.evaluate(_JS_VISUAL_READINESS)
    except Exception:  # noqa: BLE001 - readiness must not prevent a screenshot
        pass


_JS_VISUAL_READINESS = r"""
(async () => {
  const VISUAL_READINESS_TIMEOUT_MS = 1100;
  const domReady = document.readyState === 'loading'
    ? new Promise((resolve) => document.addEventListener(
        'DOMContentLoaded', resolve, {once: true}))
    : Promise.resolve();
  const assetsReady = (async () => {
    await domReady;
    const fontsReady = document.fonts?.ready || Promise.resolve();
    const images = Array.from(document.images || [])
      .filter((image) => {
        if (image.complete) return false;
        const rect = image.getBoundingClientRect();
        return rect.bottom > 0 && rect.right > 0
          && rect.top < innerHeight && rect.left < innerWidth;
      })
      .slice(0, 12)
      .map((image) => new Promise((resolve) => {
        image.addEventListener('load', resolve, {once: true});
        image.addEventListener('error', resolve, {once: true});
      }));
    await Promise.allSettled([fontsReady, ...images]);
  })();
  const timedOut = await Promise.race([
    assetsReady.then(() => false),
    new Promise((resolve) => setTimeout(() => resolve(true), VISUAL_READINESS_TIMEOUT_MS)),
  ]);
  await Promise.race([
    new Promise((resolve) => requestAnimationFrame(
      () => requestAnimationFrame(resolve))),
    new Promise((resolve) => setTimeout(resolve, 150)),
  ]);
  return {timed_out: timedOut, ready_state: document.readyState};
})()
"""


_JS_PAGE_OBSERVATION = r"""
(() => {
  const LIMIT = 6000;
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const selectorHint = (element) => {
    if (element.id) {
      const escaped = globalThis.CSS?.escape ? CSS.escape(element.id) : element.id;
      return ` selector=#${escaped}`;
    }
    const name = element.getAttribute('name');
    if (name) return ` selector=${element.tagName.toLowerCase()}[name=${JSON.stringify(name)}]`;
    return '';
  };
  const roleFor = (element) => {
    const explicit = element.getAttribute('role');
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) return `heading level=${tag.slice(1)}`;
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'summary') return 'summary';
    if (tag === 'input') {
      const type = (element.getAttribute('type') || 'text').toLowerCase();
      if (['checkbox', 'radio', 'button', 'submit', 'reset'].includes(type)) return type;
      return 'textbox';
    }
    return tag;
  };
  const accessibleName = (element) => normalize(
    element.getAttribute('aria-label')
      || element.labels?.[0]?.innerText
      || element.getAttribute('alt')
      || element.innerText
      || element.getAttribute('placeholder')
      || element.getAttribute('title')
  );
  const lines = [`document ${JSON.stringify(normalize(document.title))}`];
  let estimatedLength = lines[0].length + 1;
  let truncated = false;
  const append = (line) => {
    const remaining = LIMIT - estimatedLength;
    if (remaining <= 1) {
      truncated = true;
      return false;
    }
    if (line.length + 1 > remaining) {
      lines.push(line.slice(0, remaining - 1));
      estimatedLength = LIMIT;
      truncated = true;
      return false;
    }
    lines.push(line);
    estimatedLength += line.length + 1;
    return true;
  };
  const named = new Set();
  const semanticSelector = [
    'h1','h2','h3','h4','h5','h6','a','button','input','textarea','select',
    'summary','[role]','table','dialog'
  ].join(',');
  for (const element of document.querySelectorAll(semanticSelector)) {
    if (!visible(element)) continue;
    const name = accessibleName(element);
    if (!name && !['table', 'dialog'].includes(element.tagName.toLowerCase())) continue;
    if (name) named.add(name);
    const states = [];
    if (element.disabled) states.push('disabled');
    if (element.checked) states.push('checked');
    if (element.selected) states.push('selected');
    const expanded = element.getAttribute('aria-expanded');
    if (expanded !== null) states.push(`expanded=${expanded}`);
    if (element.tagName.toLowerCase() === 'select') {
      const selected = Array.from(element.selectedOptions || []).map((item) => normalize(item.textContent));
      if (selected.length) states.push(`value=${JSON.stringify(selected.join(', '))}`);
    }
    if (!append(`[${roleFor(element)}] ${JSON.stringify(name)}${selectorHint(element)}${states.length ? ` ${states.join(' ')}` : ''}`)) break;
  }
  const rawBodyText = String(document.body?.innerText || '');
  const bodyText = normalize(rawBodyText.slice(0, 2000));
  const bodySlice = rawBodyText.slice(0, LIMIT * 4);
  if (bodySlice.length < rawBodyText.length) truncated = true;
  for (const rawLine of bodySlice.split(/\n+/)) {
    const text = normalize(rawLine);
    if (text && !named.has(text) && !append(`[text] ${JSON.stringify(text)}`)) break;
  }
  const visualElements = Array.from(document.querySelectorAll('canvas,video,img,svg'))
    .filter(visible);
  const strongVisual = visualElements.some((element) => ['CANVAS', 'VIDEO'].includes(element.tagName));
  const visualHint = strongVisual
    || (visualElements.length >= 3 && bodyText.length < 1000)
    || (visualElements.length > 0 && lines.length <= 1);
  const joined = lines.join('\n');
  return {
    snapshot: joined.slice(0, LIMIT),
    truncated,
    visual_hint: visualHint,
  };
})()
"""


_JS_TO_MARKDOWN = """
(() => {
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName.toLowerCase();
    const children = Array.from(node.childNodes).map(walk).join('');
    if (tag === 'a') {
      const href = node.getAttribute('href');
      return href ? `[${children}](${href})` : children;
    }
    if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
      const level = '#'.repeat(parseInt(tag[1]));
      return `\\n${level} ${children}\\n`;
    }
    if (tag === 'p') return `\\n${children}\\n`;
    if (tag === 'br') return '\\n';
    if (tag === 'li') return `\\n- ${children}`;
    if (['script','style','nav','footer','aside'].includes(tag)) return '';
    return children;
  };
  return walk(document.body);
})()
"""

_JS_TO_MARKDOWN_ELEMENT = """
(el) => {
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName.toLowerCase();
    const children = Array.from(node.childNodes).map(walk).join('');
    if (tag === 'a') {
      const href = node.getAttribute('href');
      return href ? `[${children}](${href})` : children;
    }
    if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
      const level = '#'.repeat(parseInt(tag[1]));
      return `\\n${level} ${children}\\n`;
    }
    if (tag === 'p') return `\\n${children}\\n`;
    if (tag === 'br') return '\\n';
    if (tag === 'li') return `\\n- ${children}`;
    if (['script','style','nav','footer','aside'].includes(tag)) return '';
    return children;
  };
  return walk(el);
}
"""


__all__ = [
    "BrowserRuntimeConfig",
    "BrowserSession",
    "PlaywrightBrowserBackend",
    "playwright_available",
]
