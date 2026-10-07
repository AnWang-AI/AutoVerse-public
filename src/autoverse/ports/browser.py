"""Browser automation port shared by core and frontend/infra adapters.

The authoritative browser page may live in a headless infra adapter or in a
native frontend process.  Core only depends on this behavioral contract.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from autoverse.types import BrowserPageObservation


class BrowserError(RuntimeError):
    """Expected browser failure surfaced as actionable tool feedback."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "browser_error",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.metadata = metadata or {}


@runtime_checkable
class BrowserPort(Protocol):
    """Session-scoped operations required by the built-in browser tools."""

    async def launch(self) -> None: ...

    async def close(self) -> None: ...

    async def list_pages(self) -> list[dict[str, Any]]: ...

    async def new_page(
        self,
        url: str | None = None,
        *,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]: ...

    async def switch_page(self, page_id: str) -> dict[str, Any]: ...

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]: ...

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes: ...

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation: ...

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        """Evaluate page JavaScript and return its JSON-compatible final value.

        Adapters may support asynchronous function bodies, but they must await
        those bodies before returning.  Promise, function, DOM-node, and other
        host-specific objects must never cross this port: adapters either await
        a supported asynchronous body or reject the result as actionable
        ``BrowserError`` feedback.
        """
        ...

    async def click(
        self,
        selector: str | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        clear: bool = True,
        press_key: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_content(
        self,
        *,
        selector: str | None = None,
        mode: str = "text",
        page_id: str | None = None,
    ) -> str: ...

    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: float = 500,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def wait(
        self,
        *,
        condition: str = "visible",
        selector: str | None = None,
        timeout: float = 10000,
        page_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def select(
        self,
        selector: str,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        page_id: str | None = None,
    ) -> list[str]: ...

    async def keyboard(
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]: ...


__all__ = ["BrowserError", "BrowserPort"]
