"""Codec unit tests for MCPSession against a FakeTransport (P6).

These tests exercise the JSON-RPC 2.0 codec in isolation: a ``FakeTransport``
yields pre-recorded reply frames and records the request frames the session
emits. No subprocess is spawned, proving transport is independent from the codec
(docs/reference/mcp.md).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from autoverse.infra.mcp.session import MCPError, MCPSession
from autoverse.infra.mcp.transport import MCPTransport


class FakeTransport:
    """In-memory transport: replies are pre-recorded newline-delimited frames.

    ``replies`` is consumed FIFO on each ``recv``. ``sent`` records every frame
    the session writes so tests can assert request shape.
    """

    def __init__(self, replies: list[bytes]) -> None:
        self._replies = list(replies)
        self.sent: list[bytes] = []
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    async def recv(self) -> bytes:
        if not self._replies:
            raise EOFError("no more frames")
        return self._replies.pop(0)

    async def close(self) -> None:
        self.closed = True


class HangingTransport(FakeTransport):
    """Transport whose response never arrives within the session timeout."""

    async def recv(self) -> bytes:
        await asyncio.sleep(1)
        return b""


def _frame(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _result(req_id: int, result: Any) -> bytes:
    return _frame({"jsonrpc": "2.0", "id": req_id, "result": result})


def test_fake_transport_satisfies_protocol() -> None:
    assert isinstance(FakeTransport([]), MCPTransport)


async def test_initialize_handshake_decodes() -> None:
    transport = FakeTransport(
        [_result(1, {"protocolVersion": "2024-11-05", "capabilities": {}})]
    )
    session = MCPSession("srv", transport)
    info = await session.initialize()
    assert info["protocolVersion"] == "2024-11-05"
    # Request shape: JSON-RPC initialize with clientInfo + protocolVersion.
    req = json.loads(transport.sent[0])
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "initialize"
    assert "protocolVersion" in req["params"]
    assert "clientInfo" in req["params"]


async def test_list_tools_decodes_into_defs() -> None:
    payload = {
        "tools": [
            {
                "name": "search",
                "description": "find things",
                "inputSchema": {"type": "object", "properties": {"q": {}}},
            },
            {"name": "bare"},
        ]
    }
    transport = FakeTransport([_result(1, payload)])
    session = MCPSession("srv", transport)
    defs = await session.list_tools()
    assert [d.name for d in defs] == ["search", "bare"]
    assert defs[0].description == "find things"
    assert defs[0].input_schema == {"type": "object", "properties": {"q": {}}}
    assert defs[1].description == ""
    assert defs[1].input_schema == {}
    req = json.loads(transport.sent[0])
    assert req["method"] == "tools/list"


async def test_call_tool_returns_content_blocks() -> None:
    payload = {"content": [{"type": "text", "text": "hit-42"}]}
    transport = FakeTransport([_result(1, payload)])
    session = MCPSession("srv", transport)
    blocks = await session.call_tool("search", {"q": "bug"})
    assert blocks == [{"type": "text", "text": "hit-42"}]
    req = json.loads(transport.sent[0])
    assert req["method"] == "tools/call"
    assert req["params"] == {"name": "search", "arguments": {"q": "bug"}}


async def test_list_resources_decodes() -> None:
    payload = {
        "resources": [
            {
                "uri": "file:///a.txt",
                "name": "A",
                "description": "first",
                "mimeType": "text/plain",
            }
        ]
    }
    transport = FakeTransport([_result(1, payload)])
    session = MCPSession("srv", transport)
    res = await session.list_resources()
    assert len(res) == 1
    assert res[0].uri == "file:///a.txt"
    assert res[0].name == "A"
    assert res[0].description == "first"
    assert res[0].mime_type == "text/plain"
    assert json.loads(transport.sent[0])["method"] == "resources/list"


async def test_read_resource_returns_contents() -> None:
    payload = {"contents": [{"uri": "file:///a.txt", "text": "hello"}]}
    transport = FakeTransport([_result(1, payload)])
    session = MCPSession("srv", transport)
    contents = await session.read_resource("file:///a.txt")
    assert contents == [{"uri": "file:///a.txt", "text": "hello"}]
    req = json.loads(transport.sent[0])
    assert req["method"] == "resources/read"
    assert req["params"] == {"uri": "file:///a.txt"}


async def test_error_response_raises_mcperror() -> None:
    transport = FakeTransport(
        [_frame({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}})]
    )
    session = MCPSession("srv", transport)
    with pytest.raises(MCPError) as ei:
        await session.list_tools()
    assert "nope" in str(ei.value)


async def test_request_timeout_raises_mcperror() -> None:
    transport = HangingTransport([])
    session = MCPSession("srv", transport, rpc_timeout=0.01)

    with pytest.raises(MCPError) as ei:
        await session.list_tools()

    assert "timed out" in str(ei.value)
    assert "tools/list" in str(ei.value)
    assert transport.sent


async def test_skips_unrelated_id_frames() -> None:
    # A notification / mismatched-id frame arrives before the matching reply.
    transport = FakeTransport(
        [
            _frame({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}),
            _result(1, {"tools": []}),
        ]
    )
    session = MCPSession("srv", transport)
    defs = await session.list_tools()
    assert defs == []


async def test_request_ids_monotonic() -> None:
    transport = FakeTransport(
        [_result(1, {"tools": []}), _result(2, {"tools": []})]
    )
    session = MCPSession("srv", transport)
    await session.list_tools()
    await session.list_tools()
    ids = [json.loads(f)["id"] for f in transport.sent]
    assert ids == [1, 2]


async def test_close_closes_transport() -> None:
    transport = FakeTransport([])
    session = MCPSession("srv", transport)
    await session.close()
    assert transport.closed is True
