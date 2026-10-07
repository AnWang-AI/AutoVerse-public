"""Unit: URL guard enforcement inside web_fetch / browser_navigate tools."""

from __future__ import annotations

from typing import Any, Literal

from autoverse.core.tools.builtin.browser import (
    BrowserBackend,
    BrowserClosePageArgs,
    BrowserClosePageTool,
    BrowserListPagesArgs,
    BrowserListPagesTool,
    BrowserNavigateTool,
    BrowserNewPageArgs,
    BrowserNewPageTool,
    BrowserSwitchPageArgs,
    BrowserSwitchPageTool,
)
from autoverse.core.tools.builtin.web import WebFetchTool
from autoverse.types import BrowserPageObservation

from tests.conftest import make_ctx


async def test_web_fetch_blocks_file_scheme(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    res = await WebFetchTool().run(
        WebFetchTool.Args(url="file:///etc/passwd"), ctx
    )
    assert res.is_error and res.error_kind == "blocked_url"


async def test_web_fetch_blocks_metadata_ip(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    res = await WebFetchTool().run(
        WebFetchTool.Args(url="http://169.254.169.254/latest/meta-data/"), ctx
    )
    assert res.is_error and res.error_kind == "blocked_url"


class _RecordingBackend(BrowserBackend):
    def __init__(self) -> None:
        self.navigated: list[str] = []
        self.page_targets: list[str | None] = []
        self.active_page_id: str | None = "page-a"
        self.pages: list[dict[str, Any]] = [
            {
                "page_id": "page-a",
                "title": "First",
                "url": "about:blank",
                "active": True,
            }
        ]

    async def launch(self) -> None: ...

    async def close(self) -> None: ...

    async def list_pages(self) -> list[dict[str, Any]]:
        return self.pages

    async def new_page(
        self, url: str | None = None, *, wait_until: str = "domcontentloaded"
    ) -> dict[str, Any]:
        page_id = f"page-{len(self.pages) + 1}"
        for page in self.pages:
            page["active"] = False
        page = {
            "page_id": page_id,
            "title": "New",
            "url": url or "about:blank",
            "active": True,
        }
        self.pages.append(page)
        self.active_page_id = page_id
        return page

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        self.active_page_id = page_id
        for page in self.pages:
            page["active"] = page["page_id"] == page_id
        return next(page for page in self.pages if page["active"])

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        closed = page_id or self.active_page_id
        self.pages = [page for page in self.pages if page["page_id"] != closed]
        self.active_page_id = self.pages[0]["page_id"] if self.pages else None
        for page in self.pages:
            page["active"] = page["page_id"] == self.active_page_id
        return {
            "closed_page_id": closed,
            "active_page_id": self.active_page_id,
            "page_count": len(self.pages),
        }

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        self.navigated.append(url)
        self.page_targets.append(page_id)
        if page_id is not None:
            self.active_page_id = page_id
        return {"url": url, "page_id": self.active_page_id}

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        return b""

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        target_id = page_id or self.active_page_id or "page-none"
        page = next(
            (item for item in self.pages if item["page_id"] == target_id),
            {},
        )
        return BrowserPageObservation(
            page_id=target_id,
            revision=1,
            url=str(page.get("url") or "about:blank"),
            title=str(page.get("title") or "Untitled"),
            is_loading=bool(page.get("is_loading", False)),
            active=target_id == self.active_page_id,
            semantic_snapshot="document \"Fixture\"",
        )

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
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
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        return {}


async def test_browser_navigate_blocks_loopback(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    ctx.app.browser = backend  # type: ignore[attr-defined]
    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(url="http://127.0.0.1:8080/"), ctx
    )
    assert res.is_error and res.error_kind == "blocked_url"
    assert backend.navigated == []  # never reached the backend


async def test_browser_page_tools_preserve_stable_targets(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    ctx.app.browser = backend  # type: ignore[attr-defined]
    backend.pages[0]["is_loading"] = True

    listed = await BrowserListPagesTool().run(BrowserListPagesArgs(), ctx)
    assert listed.metadata["active_page_id"] == "page-a"
    assert listed.metadata["page_count"] == 1
    assert "[loading]" in listed.output

    created = await BrowserNewPageTool().run(
        BrowserNewPageArgs(url="https://example.com", screenshot=False), ctx
    )
    page_id = created.metadata["page_id"]
    assert page_id == "page-2"
    switched = await BrowserSwitchPageTool().run(
        BrowserSwitchPageArgs(page_id="page-a", screenshot=False), ctx
    )
    assert switched.metadata["page_id"] == "page-a"

    navigated = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(
            page_id=page_id,
            url="https://example.org",
            screenshot=False,
        ),
        ctx,
    )
    assert not navigated.is_error
    assert backend.page_targets == [page_id]

    closed = await BrowserClosePageTool().run(
        BrowserClosePageArgs(page_id=page_id), ctx
    )
    assert closed.metadata["closed_page_id"] == page_id
    assert closed.metadata["active_page_id"] == "page-a"


async def test_browser_new_page_applies_url_guard(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    ctx.app.browser = backend  # type: ignore[attr-defined]

    result = await BrowserNewPageTool().run(
        BrowserNewPageArgs(url="http://169.254.169.254/", screenshot=False), ctx
    )

    assert result.is_error and result.error_kind == "blocked_url"
    assert len(backend.pages) == 1


async def test_visible_native_browser_can_navigate_to_loopback(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(
            url="http://127.0.0.1:8080/", screenshot=False
        ),
        ctx,
    )

    assert not res.is_error
    assert backend.navigated == ["http://127.0.0.1:8080/"]


async def test_visible_native_browser_allows_legacy_numeric_loopback(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(url="http://2130706433:8080/", screenshot=False),
        ctx,
    )

    assert not res.is_error


async def test_visible_native_browser_blocks_legacy_numeric_private_ip(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(url="http://167772161/", screenshot=False),
        ctx,
    )

    assert res.is_error and res.error_kind == "blocked_url"
    assert backend.navigated == []


async def test_visible_native_browser_allows_localhost_subdomain(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(url="http://app.localhost:8080/", screenshot=False),
        ctx,
    )

    assert not res.is_error


async def test_visible_native_browser_blocks_internal_hostname(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    for url in (
        "http://printer.local/",
        "http://service.internal/",
        "http://router.lan/",
        "http://metadata.google.internal/",
    ):
        res = await BrowserNavigateTool().run(
            BrowserNavigateTool.Args(url=url, screenshot=False), ctx
        )
        assert res.is_error and res.error_kind == "blocked_url"

    assert backend.navigated == []


async def test_visible_native_browser_does_not_overblock_public_literal_ips(
    tmp_path,
) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    backend.allows_loopback = True  # type: ignore[attr-defined]
    ctx.app.browser = backend  # type: ignore[attr-defined]

    urls = (
        "https://192.0.0.9/",
        "https://192.31.196.1/",
        "https://198.51.1.1/",
        "https://203.0.114.1/",
    )
    for url in urls:
        result = await BrowserNavigateTool().run(
            BrowserNavigateTool.Args(url=url, screenshot=False), ctx
        )
        assert not result.is_error

    assert backend.navigated == list(urls)


async def test_browser_navigate_allows_public(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    backend = _RecordingBackend()
    ctx.app.browser = backend  # type: ignore[attr-defined]
    res = await BrowserNavigateTool().run(
        BrowserNavigateTool.Args(url="https://example.com/"), ctx
    )
    assert res.succeeded and backend.navigated == ["https://example.com/"]
