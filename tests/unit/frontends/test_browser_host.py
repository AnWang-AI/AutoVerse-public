"""Unit coverage for the native WebKit browser-host bridge."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from autoverse.frontends.web.browser_host import NativeBrowserBroker
from autoverse.ports import BrowserError, BrowserPort


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.close_calls: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, value: dict[str, Any]) -> None:
        await self.sent.put(value)

    async def receive_json(self) -> dict[str, Any]:
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return value

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))
        await self.incoming.put(None)


async def _accept(
    broker: NativeBrowserBroker,
    *,
    on_connected: Any = None,
) -> tuple[_FakeWebSocket, asyncio.Task[None]]:
    websocket = _FakeWebSocket()
    task = asyncio.create_task(
        broker.serve(  # type: ignore[arg-type]
            websocket, on_connected=on_connected
        )
    )
    while not websocket.accepted:
        await asyncio.sleep(0)
    return websocket, task


async def _connect(
    broker: NativeBrowserBroker,
) -> tuple[_FakeWebSocket, asyncio.Task[None]]:
    websocket, task = await _accept(broker)
    await websocket.incoming.put(
        {"type": "hello", "protocol": 1, "engine": "webkit"}
    )
    assert await websocket.sent.get() == {
        "type": "hello_ack",
        "protocol": 1,
        "engine": "webkit",
    }
    return websocket, task


async def _round_trip(
    websocket: _FakeWebSocket,
    operation: Awaitable[Any],
    *,
    action: str,
    params: dict[str, Any],
    result: Any,
) -> Any:
    task = asyncio.ensure_future(operation)
    command = await websocket.sent.get()
    assert command["action"] == action
    assert command["params"] == params
    await websocket.incoming.put(
        {"type": "result", "id": command["id"], "ok": True, "result": result}
    )
    return await task


async def test_session_maps_commands_and_decodes_screenshot() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("session-1")
    assert isinstance(session, BrowserPort)
    assert session.allows_loopback  # type: ignore[attr-defined]

    navigate_task = asyncio.create_task(
        session.navigate("http://127.0.0.1:8000", wait_until="load")
    )
    command = await websocket.sent.get()
    assert command == {
        "type": "command",
        "id": command["id"],
        "session_id": "session-1",
        "action": "navigate",
        "params": {
            "url": "http://127.0.0.1:8000",
            "wait_until": "load",
        },
    }
    await websocket.incoming.put(
        {
            "type": "result",
            "id": command["id"],
            "ok": True,
            "result": {"url": "http://127.0.0.1:8000/", "title": "Local"},
        }
    )
    assert (await navigate_task)["title"] == "Local"

    screenshot_task = asyncio.create_task(session.screenshot())
    screenshot_command = await websocket.sent.get()
    payload = b"native-png"
    await websocket.incoming.put(
        {
            "type": "result",
            "id": screenshot_command["id"],
            "ok": True,
            "result": {"data": base64.b64encode(payload).decode("ascii")},
        }
    )
    assert await screenshot_task == payload

    await websocket.incoming.put(None)
    await serve_task
    assert not broker.connected


async def test_session_maps_the_complete_native_command_surface() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("all-actions")

    await _round_trip(websocket, session.launch(), action="launch", params={}, result={})
    assert await _round_trip(
        websocket,
        session.list_pages(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-a",
                    "title": "First",
                    "url": "https://first.example",
                    "active": True,
                }
            ],
            "active_page_id": "page-a",
        },
    ) == [
        {
            "page_id": "page-a",
            "title": "First",
            "url": "https://first.example",
            "active": True,
        }
    ]
    assert session.active_page_id == "page-a"
    observed = await _round_trip(
        websocket,
        session.observe_page(page_id="page-a", screenshot="always"),
        action="observe",
        params={"page_id": "page-a", "screenshot": "always"},
        result={
            "page_id": "page-a",
            "revision": 4,
            "url": "https://first.example",
            "title": "First",
            "is_loading": False,
            "active": True,
            "semantic_snapshot": '[heading level=1] "First"',
            "semantic_truncated": False,
            "visual_hint": False,
            "screenshot_data": base64.b64encode(b"observed-png").decode("ascii"),
            "screenshot_media_type": "image/png",
        },
    )
    assert observed.page_id == "page-a"
    assert observed.revision == 4
    assert observed.semantic_snapshot.endswith('"First"')
    assert observed.screenshot == b"observed-png"
    assert await _round_trip(
        websocket,
        session.new_page("https://second.example", wait_until="load"),
        action="new_page",
        params={"url": "https://second.example", "wait_until": "load"},
        result={"page_id": "page-b", "title": "Second", "active": True},
    ) == {"page_id": "page-b", "title": "Second", "active": True}
    assert await _round_trip(
        websocket,
        session.switch_page("page-a"),
        action="switch_page",
        params={"page_id": "page-a"},
        result={"page_id": "page-a", "title": "First", "active": True},
    ) == {"page_id": "page-a", "title": "First", "active": True}
    assert await _round_trip(
        websocket,
        session.evaluate("return document.title", page_id="page-a"),
        action="evaluate",
        params={"script": "return document.title", "page_id": "page-a"},
        result={"value": "Fixture", "page_id": "page-a"},
    ) == "Fixture"
    assert await _round_trip(
        websocket,
        session.click("#run", button="right", double=True),
        action="click",
        params={
            "selector": "#run",
            "x": None,
            "y": None,
            "button": "right",
            "double": True,
        },
        result={"url": "https://example.com"},
    ) == {"url": "https://example.com"}
    assert await _round_trip(
        websocket,
        session.type_text("#name", "Ada", clear=False, press_key="Enter"),
        action="type",
        params={
            "selector": "#name",
            "text": "Ada",
            "clear": False,
            "press_key": "Enter",
        },
        result={"title": "Typed"},
    ) == {"title": "Typed"}
    assert await _round_trip(
        websocket,
        session.get_content(selector="#main", mode="markdown"),
        action="content",
        params={"selector": "#main", "mode": "markdown"},
        result={"content": "# Fixture"},
    ) == "# Fixture"
    assert await _round_trip(
        websocket,
        session.scroll(direction="left", amount=123, selector="#rail"),
        action="scroll",
        params={"direction": "left", "amount": 123, "selector": "#rail"},
        result={"url": "https://example.com"},
    ) == {"url": "https://example.com"}
    assert await _round_trip(
        websocket,
        session.wait(condition="hidden", selector="#spinner", timeout=2500),
        action="wait",
        params={"condition": "hidden", "selector": "#spinner", "timeout": 2500},
        result={"url": "https://example.com"},
    ) == {"url": "https://example.com"}
    assert await _round_trip(
        websocket,
        session.select("#choice", label="Beta"),
        action="select",
        params={
            "selector": "#choice",
            "label": "Beta",
            "value": None,
            "index": None,
        },
        result=["b"],
    ) == ["b"]
    assert await _round_trip(
        websocket,
        session.keyboard("Control+A", selector="#name"),
        action="keyboard",
        params={"key": "Control+A", "selector": "#name"},
        result={"title": "Keyboard"},
    ) == {"title": "Keyboard"}
    assert await _round_trip(
        websocket,
        session.close_page("page-b"),
        action="close_page",
        params={"page_id": "page-b"},
        result={"closed_page_id": "page-b", "active_page_id": "page-a"},
    ) == {"closed_page_id": "page-b", "active_page_id": "page-a"}
    await _round_trip(websocket, session.close(), action="close", params={}, result={})

    await websocket.incoming.put(None)
    await serve_task


async def test_session_reports_initial_native_pages_to_a_new_agent_session() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("initial-pages")

    initial = await _round_trip(
        websocket,
        session.poll_page_state_change(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-a",
                    "title": "Already open",
                    "url": "https://a.test",
                    "is_loading": False,
                    "active": True,
                }
            ],
            "active_page_id": "page-a",
        },
    )

    assert initial == {
        "initial_state": True,
        "previous_active_page_id": None,
        "active_page_id": "page-a",
        "page_count": 1,
        "opened_page_ids": ["page-a"],
        "closed_page_ids": [],
        "updated_page_ids": [],
    }

    await websocket.incoming.put(None)
    await serve_task


async def test_session_detects_active_same_page_and_collection_changes() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("page-drift")

    assert await _round_trip(
        websocket,
        session.list_pages(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-a",
                    "title": "Alpha",
                    "url": "https://a.test",
                    "is_loading": False,
                    "active": True,
                },
                {
                    "page_id": "page-b",
                    "title": "Beta",
                    "url": "https://b.test",
                    "is_loading": False,
                    "active": False,
                },
            ],
            "active_page_id": "page-a",
        },
    )

    changed = await _round_trip(
        websocket,
        session.poll_page_state_change(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-a",
                    "title": "Alpha",
                    "url": "https://a.test",
                    "is_loading": False,
                    "active": False,
                },
                {
                    "page_id": "page-b",
                    "title": "Beta",
                    "url": "https://b.test",
                    "is_loading": False,
                    "active": True,
                },
            ],
            "active_page_id": "page-b",
        },
    )
    assert changed == {
        "initial_state": False,
        "previous_active_page_id": "page-a",
        "active_page_id": "page-b",
        "page_count": 2,
        "opened_page_ids": [],
        "closed_page_ids": [],
        "updated_page_ids": [],
    }
    assert session.active_page_id == "page-b"

    same_page_navigation = await _round_trip(
        websocket,
        session.poll_page_state_change(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-a",
                    "title": "Alpha",
                    "url": "https://a.test",
                    "is_loading": False,
                    "active": False,
                },
                {
                    "page_id": "page-b",
                    "revision": 2,
                    "title": "Beta 2",
                    "url": "https://b.test/next",
                    "is_loading": True,
                    "active": True,
                },
            ],
            "active_page_id": "page-b",
        },
    )
    assert same_page_navigation == {
        "initial_state": False,
        "previous_active_page_id": "page-b",
        "active_page_id": "page-b",
        "page_count": 2,
        "opened_page_ids": [],
        "closed_page_ids": [],
        "updated_page_ids": ["page-b"],
    }

    collection_change = await _round_trip(
        websocket,
        session.poll_page_state_change(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-b",
                    "revision": 3,
                    "title": "Beta 2",
                    "url": "https://b.test/next",
                    "is_loading": False,
                    "active": True,
                },
                {
                    "page_id": "page-c",
                    "title": "Gamma",
                    "url": "https://c.test",
                    "is_loading": False,
                    "active": False,
                },
            ],
            "active_page_id": "page-b",
        },
    )
    assert collection_change == {
        "initial_state": False,
        "previous_active_page_id": "page-b",
        "active_page_id": "page-b",
        "page_count": 2,
        "opened_page_ids": ["page-c"],
        "closed_page_ids": ["page-a"],
        "updated_page_ids": ["page-b"],
    }

    assert await _round_trip(
        websocket,
        session.poll_page_state_change(),
        action="list_pages",
        params={},
        result={
            "pages": [
                {
                    "page_id": "page-b",
                    "revision": 3,
                    "title": "Beta 2",
                    "url": "https://b.test/next",
                    "is_loading": False,
                    "active": True,
                },
                {
                    "page_id": "page-c",
                    "title": "Gamma",
                    "url": "https://c.test",
                    "is_loading": False,
                    "active": False,
                },
            ],
            "active_page_id": "page-b",
        },
    ) is None

    await websocket.incoming.put(None)
    await serve_task


async def test_hello_acknowledges_protocol_and_runs_connected_hook() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    calls = 0

    async def connected() -> None:
        nonlocal calls
        calls += 1

    websocket, serve_task = await _accept(broker, on_connected=connected)
    assert not broker.connected
    await websocket.incoming.put(
        {"type": "hello", "protocol": 1, "engine": "webkit"}
    )

    assert await websocket.sent.get() == {
        "type": "hello_ack",
        "protocol": 1,
        "engine": "webkit",
    }
    assert calls == 1

    await websocket.incoming.put(None)
    await serve_task


async def test_unhandshaken_socket_never_becomes_connected_or_receives_commands() -> None:
    broker = NativeBrowserBroker(command_timeout=0.01)
    websocket, serve_task = await _accept(broker)

    assert not broker.connected
    with pytest.raises(BrowserError) as raised:
        await broker.session("pre-hello").evaluate("document.title")
    assert raised.value.error_kind == "browser_unavailable"
    assert websocket.sent.empty()

    await websocket.incoming.put(None)
    await serve_task


async def test_idle_unhandshaken_socket_is_closed_at_deadline() -> None:
    broker = NativeBrowserBroker(command_timeout=1, handshake_timeout=0.01)
    websocket, serve_task = await _accept(broker)

    await serve_task

    assert not broker.connected
    assert websocket.close_calls == [
        (1008, "browser-host handshake timed out")
    ]


async def test_non_hello_noise_cannot_extend_handshake_deadline() -> None:
    broker = NativeBrowserBroker(command_timeout=1, handshake_timeout=0.03)
    websocket, serve_task = await _accept(broker)

    for _ in range(3):
        await websocket.incoming.put({"type": "noise"})
        await asyncio.sleep(0.008)
    await serve_task

    assert not broker.connected
    assert websocket.close_calls == [
        (1008, "browser-host handshake timed out")
    ]


async def test_incompatible_handshake_is_rejected_without_replacing_ready_host() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    ready, ready_task = await _connect(broker)
    incompatible, incompatible_task = await _accept(broker)

    await incompatible.incoming.put(
        {"type": "hello", "protocol": 99, "engine": "webkit"}
    )
    assert await incompatible.sent.get() == {
        "type": "hello_reject",
        "protocol": 1,
        "reason": "unsupported browser-host protocol",
    }
    await incompatible_task
    assert incompatible.close_calls == [
        (1002, "unsupported browser-host protocol")
    ]
    assert broker.connected

    pending = asyncio.create_task(broker.session("still-ready").evaluate("1 + 1"))
    command = await ready.sent.get()
    await ready.incoming.put(
        {"type": "result", "id": command["id"], "ok": True, "result": {"value": 2}}
    )
    assert await pending == 2

    await ready.incoming.put(None)
    await ready_task


async def test_native_error_is_preserved_as_browser_error() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("session-2")

    click_task = asyncio.create_task(session.click("#submit"))
    command = await websocket.sent.get()
    await websocket.incoming.put(
        {
            "type": "result",
            "id": command["id"],
            "ok": False,
            "error": {
                "kind": "user_control",
                "message": "Browser control is with the user.",
            },
        }
    )
    with pytest.raises(BrowserError) as raised:
        await click_task
    assert raised.value.error_kind == "user_control"

    await websocket.incoming.put(None)
    await serve_task


async def test_native_evaluate_result_error_preserves_actionable_contract() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    pending = asyncio.create_task(
        broker.session("unsupported-evaluate").evaluate("Promise.resolve(1)")
    )
    command = await websocket.sent.get()
    await websocket.incoming.put(
        {
            "type": "result",
            "id": command["id"],
            "ok": False,
            "error": {
                "kind": "unsupported_result",
                "message": "Use a top-level `return await ...`.",
            },
        }
    )

    with pytest.raises(BrowserError) as raised:
        await pending
    assert raised.value.error_kind == "unsupported_result"
    assert "return await" in str(raised.value)

    await websocket.incoming.put(None)
    await serve_task


async def test_request_without_host_is_actionable() -> None:
    session = NativeBrowserBroker(command_timeout=0.01).session("offline")

    with pytest.raises(BrowserError) as raised:
        await session.launch()

    assert raised.value.error_kind == "browser_unavailable"


async def test_disconnect_fails_pending_command_immediately() -> None:
    broker = NativeBrowserBroker(command_timeout=10)
    websocket, serve_task = await _connect(broker)
    session = broker.session("disconnect")
    pending = asyncio.create_task(session.get_content())
    await websocket.sent.get()

    await websocket.incoming.put(None)
    await serve_task

    with pytest.raises(BrowserError) as raised:
        await pending
    assert raised.value.error_kind == "browser_unavailable"


async def test_command_timeout_does_not_poison_next_request() -> None:
    broker = NativeBrowserBroker(command_timeout=0.01)
    websocket, serve_task = await _connect(broker)
    session = broker.session("timeout")

    with pytest.raises(BrowserError) as raised:
        await session.evaluate("1 + 1")
    assert raised.value.error_kind == "browser_timeout"
    assert not broker._pending  # noqa: SLF001 - timeout cleanup contract

    next_task = asyncio.create_task(session.evaluate("2 + 2"))
    command = await websocket.sent.get()
    # The first timed-out command is still in the fake outbound queue.
    if command["params"].get("script") == "1 + 1":
        command = await websocket.sent.get()
    await websocket.incoming.put(
        {"type": "result", "id": command["id"], "ok": True, "result": {"value": 4}}
    )
    assert await next_task == 4

    await websocket.incoming.put(None)
    await serve_task


async def test_wait_uses_native_timeout_plus_transport_grace() -> None:
    broker = NativeBrowserBroker(command_timeout=0.01)
    websocket, serve_task = await _connect(broker)
    pending = asyncio.create_task(
        broker.session("long-wait").wait(
            condition="visible", selector="#later", timeout=100
        )
    )
    command = await websocket.sent.get()
    await asyncio.sleep(0.02)
    assert not pending.done()
    await websocket.incoming.put(
        {"type": "result", "id": command["id"], "ok": True, "result": {}}
    )
    assert await pending == {}

    await websocket.incoming.put(None)
    await serve_task


async def test_new_host_replaces_old_and_fails_its_pending_command() -> None:
    broker = NativeBrowserBroker(command_timeout=10)
    first, first_task = await _connect(broker)
    session = broker.session("replace")
    pending = asyncio.create_task(session.wait(condition="load"))
    await first.sent.get()

    second, second_task = await _connect(broker)
    with pytest.raises(BrowserError) as raised:
        await pending
    assert raised.value.error_kind == "browser_unavailable"
    assert await first.sent.get() == {
        "type": "host_replaced",
        "reason": "another browser host connected",
    }
    await first_task
    assert broker.connected

    await second.incoming.put(None)
    await second_task


async def test_invalid_screenshot_payload_is_rejected() -> None:
    broker = NativeBrowserBroker(command_timeout=1)
    websocket, serve_task = await _connect(broker)
    session = broker.session("bad-image")
    pending = asyncio.create_task(session.screenshot())
    command = await websocket.sent.get()
    await websocket.incoming.put(
        {
            "type": "result",
            "id": command["id"],
            "ok": True,
            "result": {"data": "not base64!!"},
        }
    )

    with pytest.raises(BrowserError, match="invalid screenshot"):
        await pending

    await websocket.incoming.put(None)
    await serve_task
