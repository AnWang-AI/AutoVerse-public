"""Native browser-host bridge for the Apple frontend.

The WebUI server brokers a narrow JSON command protocol between session-scoped
``BrowserPort`` adapters and one connected, user-visible WKWebView host.  Page
state and credentials remain owned by the native process; Python only owns
pending command correlation and per-session operation ordering.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import WebSocket, WebSocketDisconnect

from autoverse.ports import BrowserError, BrowserPort
from autoverse.types import BrowserPageObservation


@dataclass(frozen=True)
class _NativePageSnapshot:
    """Small derived view used only to detect native state drift."""

    page_id: str
    url: str
    title: str
    is_loading: bool
    revision: int


class NativeBrowserBroker:
    """Process-wide connection broker for a single visible native browser."""

    protocol_version = 1

    def __init__(
        self, *, command_timeout: float = 45.0, handshake_timeout: float = 5.0
    ) -> None:
        self._command_timeout = command_timeout
        self._handshake_timeout = handshake_timeout
        self._websocket: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()
        self._handshake_lock = asyncio.Lock()
        self._generation = 0

    @property
    def connected(self) -> bool:
        """Whether a native browser host currently owns the shared page."""

        return self._websocket is not None

    def session(self, session_id: str) -> BrowserPort:
        """Return the serial, session-scoped view of this shared host."""

        return NativeBrowserSession(self, session_id=session_id)

    async def serve(
        self,
        websocket: WebSocket,
        *,
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Accept and run one host connection until it disconnects."""

        await websocket.accept()
        generation: int | None = None
        handshake_deadline = (
            asyncio.get_running_loop().time() + self._handshake_timeout
        )
        try:
            while True:
                if generation is None:
                    remaining = max(
                        0.0,
                        handshake_deadline - asyncio.get_running_loop().time(),
                    )
                    message = await asyncio.wait_for(
                        websocket.receive_json(), timeout=remaining
                    )
                else:
                    message = await websocket.receive_json()
                if isinstance(message, dict) and message.get("type") == "hello":
                    if generation is not None:
                        continue
                    if (
                        message.get("protocol") != self.protocol_version
                        or message.get("engine") != "webkit"
                    ):
                        await websocket.send_json(
                            {
                                "type": "hello_reject",
                                "protocol": self.protocol_version,
                                "reason": "unsupported browser-host protocol",
                            }
                        )
                        await websocket.close(
                            code=1002, reason="unsupported browser-host protocol"
                        )
                        return
                    async with self._handshake_lock:
                        # The callback shares SessionManager's engine-build lock.
                        # Once it returns, publishing the ready socket is
                        # synchronous, so an activation can never miss both the
                        # callback hot-swap and the connected-host build path.
                        if on_connected is not None:
                            await on_connected()
                        previous = self._websocket
                        async with self._send_lock:
                            self._websocket = websocket
                            try:
                                await websocket.send_json(
                                    {
                                        "type": "hello_ack",
                                        "protocol": self.protocol_version,
                                        "engine": "webkit",
                                    }
                                )
                            except Exception:  # noqa: BLE001 - restore prior ready host
                                self._websocket = previous
                                raise
                        self._generation += 1
                        generation = self._generation
                        self._fail_pending("Native browser host was replaced.")
                        if previous is not None and previous is not websocket:
                            with contextlib.suppress(Exception):  # noqa: BLE001
                                async with self._send_lock:
                                    await previous.send_json(
                                        {
                                            "type": "host_replaced",
                                            "reason": "another browser host connected",
                                        }
                                    )
                            with contextlib.suppress(Exception):  # noqa: BLE001
                                await previous.close(
                                    code=1012,
                                    reason="replaced by a newer browser host",
                                )
                    continue
                if (
                    generation is not None
                    and generation == self._generation
                    and self._websocket is websocket
                ):
                    self._handle_message(message)
        except TimeoutError:
            with contextlib.suppress(Exception):  # noqa: BLE001 - timeout cleanup
                await websocket.close(
                    code=1008, reason="browser-host handshake timed out"
                )
        except WebSocketDisconnect:
            pass
        except (ValueError, RuntimeError):
            with contextlib.suppress(Exception):  # noqa: BLE001 - protocol cleanup
                await websocket.close(code=1002, reason="invalid browser-host message")
        finally:
            if (
                generation is not None
                and generation == self._generation
                and self._websocket is websocket
            ):
                self._websocket = None
                self._fail_pending("Native browser host disconnected.")

    async def request(
        self, session_id: str, action: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send one correlated command and return its JSON-compatible result."""

        websocket = self._websocket
        if websocket is None:
            raise BrowserError(
                "The native browser is not connected. Reconnect the Apple app or "
                "use the headless browser backend.",
                error_kind="browser_unavailable",
            )
        command_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        command = {
            "type": "command",
            "id": command_id,
            "session_id": session_id,
            "action": action,
            "params": params or {},
        }
        try:
            async with self._send_lock:
                if self._websocket is not websocket:
                    raise BrowserError(
                        "Native browser host changed before the command was sent.",
                        error_kind="browser_unavailable",
                    )
                self._pending[command_id] = future
                await websocket.send_json(command)
            timeout = self._timeout_for(action, params or {})
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise BrowserError(
                f"Native browser command {action!r} timed out.",
                error_kind="browser_timeout",
            ) from exc
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise BrowserError(
                "Could not communicate with the native browser host.",
                error_kind="browser_unavailable",
                metadata={"detail": str(exc)},
            ) from exc
        finally:
            self._pending.pop(command_id, None)

    def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict) or message.get("type") != "result":
            return
        command_id = str(message.get("id") or "")
        future = self._pending.get(command_id)
        if future is None or future.done():
            return
        if bool(message.get("ok")):
            future.set_result(message.get("result"))
            return
        error = message.get("error")
        payload = error if isinstance(error, dict) else {}
        future.set_exception(
            BrowserError(
                str(payload.get("message") or "Native browser command failed."),
                error_kind=str(payload.get("kind") or "browser_error"),
                metadata={
                    str(key): value
                    for key, value in payload.items()
                    if key not in {"message", "kind"}
                },
            )
        )

    def _timeout_for(self, action: str, params: dict[str, Any]) -> float:
        """Leave a grace window beyond timeouts enforced by the native host."""

        if action == "navigate":
            return max(self._command_timeout, 35.0)
        if action == "wait":
            try:
                native_timeout = max(0.0, float(params.get("timeout", 0))) / 1000
            except (TypeError, ValueError):
                native_timeout = 0.0
            return max(self._command_timeout, native_timeout + 5.0)
        return self._command_timeout

    def _fail_pending(self, message: str) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(
                    BrowserError(message, error_kind="browser_unavailable")
                )


class NativeBrowserSession:
    """Session-scoped ``BrowserPort`` proxy over a native browser host."""

    allows_loopback = True
    close_on_engine_shutdown = False
    native_browser_session = True

    def __init__(self, broker: NativeBrowserBroker, *, session_id: str) -> None:
        self._broker = broker
        self._session_id = session_id
        self._operation_lock = asyncio.Lock()
        self._active_page_id: str | None = None
        self._page_snapshots: dict[str, _NativePageSnapshot] = {}
        self._state_initialized = False

    @property
    def active_page_id(self) -> str | None:
        return self._active_page_id

    async def _request(self, action: str, **params: Any) -> Any:
        async with self._operation_lock:
            return await self._broker.request(self._session_id, action, params)

    async def launch(self) -> None:
        result = await self._request("launch")
        if isinstance(result, dict):
            self._remember_page(result)

    async def close(self) -> None:
        if self._broker.connected:
            result = await self._request("close")
            if isinstance(result, dict):
                self._remember_page(result)

    async def list_pages(self) -> list[dict[str, Any]]:
        result = _dict_result(await self._request("list_pages"))
        self._remember_page(result)
        pages = result.get("pages")
        if not isinstance(pages, list):
            return []
        return [
            {str(key): value for key, value in page.items()}
            for page in pages
            if isinstance(page, dict)
        ]

    async def poll_page_state_change(self) -> dict[str, Any] | None:
        """Refresh native pages and report drift since the last tool result.

        The host remains authoritative.  This proxy retains only compact page
        snapshots so same-tab navigation, late redirects, loading transitions,
        page creation/closure, and active-page switches are all visible at the
        next turn boundary.  The first non-empty poll reports the initial
        synchronized state instead of silently establishing a baseline.
        """

        initialized = self._state_initialized
        previous_active = self._active_page_id
        previous_pages = dict(self._page_snapshots)
        pages = await self.list_pages()
        if not initialized:
            if not pages and self._active_page_id is None:
                return None
            return {
                "initial_state": True,
                "previous_active_page_id": None,
                "active_page_id": self._active_page_id,
                "page_count": len(pages),
                "opened_page_ids": sorted(self._page_snapshots),
                "closed_page_ids": [],
                "updated_page_ids": [],
            }
        current_pages = self._page_snapshots
        previous_ids = set(previous_pages)
        current_ids = set(current_pages)
        opened = sorted(current_ids - previous_ids)
        closed = sorted(previous_ids - current_ids)
        updated = sorted(
            page_id
            for page_id in previous_ids & current_ids
            if previous_pages[page_id] != current_pages[page_id]
        )
        if (
            previous_active == self._active_page_id
            and not opened
            and not closed
            and not updated
        ):
            return None
        return {
            "initial_state": False,
            "previous_active_page_id": previous_active,
            "active_page_id": self._active_page_id,
            "page_count": len(pages),
            "opened_page_ids": opened,
            "closed_page_ids": closed,
            "updated_page_ids": updated,
        }

    async def poll_external_page_change(self) -> dict[str, Any] | None:
        """Backward-compatible alias for the generalized state poll."""

        return await self.poll_page_state_change()

    async def new_page(
        self,
        url: str | None = None,
        *,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request("new_page", url=url, wait_until=wait_until)
        )
        self._remember_page(result)
        return result

    async def switch_page(self, page_id: str) -> dict[str, Any]:
        result = _dict_result(await self._request("switch_page", page_id=page_id))
        self._remember_page(result)
        return result

    async def close_page(self, page_id: str | None = None) -> dict[str, Any]:
        result = _dict_result(
            await self._request("close_page", **_page_params(page_id))
        )
        self._remember_page(result)
        return result

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request(
                "navigate", **_page_params(page_id, url=url, wait_until=wait_until)
            )
        )
        self._remember_page(result)
        return result

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> bytes:
        result = _dict_result(
            await self._request(
                "screenshot",
                **_page_params(
                    page_id, full_page=full_page, selector=selector
                ),
            )
        )
        self._remember_page(result)
        encoded = result.get("data")
        if not isinstance(encoded, str):
            raise BrowserError("Native browser returned no screenshot data.")
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise BrowserError("Native browser returned invalid screenshot data.") from exc

    async def observe_page(
        self,
        *,
        page_id: str | None = None,
        screenshot: Literal["never", "always", "when_visual"] = "never",
    ) -> BrowserPageObservation:
        result = _dict_result(
            await self._request(
                "observe",
                **_page_params(page_id, screenshot=screenshot),
            )
        )
        self._remember_page(result)
        return _page_observation(result)

    async def evaluate(self, script: str, *, page_id: str | None = None) -> Any:
        result = await self._request(
            "evaluate", **_page_params(page_id, script=script)
        )
        if isinstance(result, dict):
            self._remember_page(result)
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

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
        result = _dict_result(
            await self._request(
                "click",
                **_page_params(
                    page_id,
                    selector=selector,
                    x=x,
                    y=y,
                    button=button,
                    double=double,
                ),
            )
        )
        self._remember_page(result)
        return result

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        clear: bool = True,
        press_key: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request(
                "type",
                **_page_params(
                    page_id,
                    selector=selector,
                    text=text,
                    clear=clear,
                    press_key=press_key,
                ),
            )
        )
        self._remember_page(result)
        return result

    async def get_content(
        self,
        *,
        selector: str | None = None,
        mode: str = "text",
        page_id: str | None = None,
    ) -> str:
        result = await self._request(
            "content", **_page_params(page_id, selector=selector, mode=mode)
        )
        if isinstance(result, dict):
            self._remember_page(result)
            return str(result.get("content") or "")
        return str(result or "")

    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: float = 500,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request(
                "scroll",
                **_page_params(
                    page_id,
                    direction=direction,
                    amount=amount,
                    selector=selector,
                ),
            )
        )
        self._remember_page(result)
        return result

    async def wait(
        self,
        *,
        condition: str = "visible",
        selector: str | None = None,
        timeout: float = 10000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request(
                "wait",
                **_page_params(
                    page_id,
                    condition=condition,
                    selector=selector,
                    timeout=timeout,
                ),
            )
        )
        self._remember_page(result)
        return result

    async def select(
        self,
        selector: str,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        page_id: str | None = None,
    ) -> list[str]:
        result = await self._request(
            "select",
            **_page_params(
                page_id,
                selector=selector,
                label=label,
                value=value,
                index=index,
            ),
        )
        if isinstance(result, dict):
            self._remember_page(result)
        if isinstance(result, list):
            return [str(item) for item in result]
        if isinstance(result, dict) and isinstance(result.get("values"), list):
            return [str(item) for item in result["values"]]
        return []

    async def keyboard(
        self,
        key: str,
        *,
        selector: str | None = None,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        result = _dict_result(
            await self._request(
                "keyboard", **_page_params(page_id, key=key, selector=selector)
            )
        )
        self._remember_page(result)
        return result

    def _remember_page(self, result: dict[str, Any]) -> None:
        active = result.get("active_page_id") or result.get("page_id")
        if isinstance(active, str) and active:
            self._active_page_id = active
        elif "active_page_id" in result and active is None:
            self._active_page_id = None

        pages = result.get("pages")
        if isinstance(pages, list):
            snapshots = {
                snapshot.page_id: snapshot
                for page in pages
                if isinstance(page, dict)
                and (snapshot := _page_snapshot(page)) is not None
            }
            self._page_snapshots = snapshots
            self._state_initialized = True
            return

        closed = result.get("closed_page_id")
        if isinstance(closed, str) and closed:
            self._page_snapshots.pop(closed, None)
            self._state_initialized = True

        snapshot = _page_snapshot(result)
        if snapshot is not None:
            self._page_snapshots[snapshot.page_id] = snapshot
            self._state_initialized = True


def _dict_result(result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        return {str(key): value for key, value in result.items()}
    return {}


def _page_snapshot(value: dict[str, Any]) -> _NativePageSnapshot | None:
    page_id = value.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        return None
    return _NativePageSnapshot(
        page_id=page_id,
        url=str(value.get("url") or ""),
        title=str(value.get("title") or ""),
        is_loading=bool(value.get("is_loading", False)),
        revision=int(value.get("revision") or 0),
    )


def _page_observation(value: dict[str, Any]) -> BrowserPageObservation:
    page_id = value.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise BrowserError("Native browser returned an observation without a page id.")
    image: bytes | None = None
    encoded = value.get("screenshot_data")
    if isinstance(encoded, str) and encoded:
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise BrowserError(
                "Native browser returned invalid observation screenshot data."
            ) from exc
    screenshot_error = value.get("screenshot_error")
    return BrowserPageObservation(
        page_id=page_id,
        revision=int(value.get("revision") or 0),
        url=str(value.get("url") or ""),
        title=str(value.get("title") or ""),
        is_loading=bool(value.get("is_loading", False)),
        active=bool(value.get("active", False)),
        semantic_snapshot=str(value.get("semantic_snapshot") or ""),
        semantic_truncated=bool(value.get("semantic_truncated", False)),
        visual_hint=bool(value.get("visual_hint", False)),
        screenshot=image,
        screenshot_media_type=(
            str(value.get("screenshot_media_type") or "image/png")
            if image is not None
            else None
        ),
        screenshot_error=(
            str(screenshot_error) if screenshot_error not in (None, "") else None
        ),
    )


def _page_params(page_id: str | None, **params: Any) -> dict[str, Any]:
    """Add an explicit page target without changing legacy active-page payloads."""

    if page_id is not None:
        params["page_id"] = page_id
    return params


__all__ = ["NativeBrowserBroker", "NativeBrowserSession"]
