"""MCP protocol layer — JSON-RPC 2.0 codec, decoupled from transport (P6).

``MCPSession`` speaks JSON-RPC 2.0 over an injected :class:`MCPTransport`
(defined in ``transport.py``). It knows nothing about *how* bytes move
(subprocess stdio, HTTP, …) — only the wire framing of requests/responses.
This is the §4.7 boundary that lets ``FakeTransport`` unit-test the codec with
pre-recorded frames and no real process.

Framing is **newline-delimited JSON** (one JSON object per line) — chosen for
simplicity over Content-Length framing; the transport owns line splitting, the
session owns the JSON object semantics.

The shared value types ``MCPError`` / ``MCPToolDef`` / ``MCPResourceDef`` are
defined here (the protocol layer is their natural home) and re-exported by
``manager`` / the package ``__init__`` for backward-compatible imports.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autoverse.infra.mcp.transport import MCPTransport

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "autoverse", "version": "0"}


class MCPError(Exception):
    """Protocol-level MCP error surfaced as actionable tool feedback (P5)."""


@dataclass(frozen=True)
class MCPToolDef:
    """A tool advertised by a server's ``tools/list`` — the schema source (P1)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPResourceDef:
    """A resource advertised by a server's ``resources/list``."""

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


class MCPSession:
    """JSON-RPC 2.0 codec over a transport (docs/reference/mcp.md).

    ``rpc_timeout`` is the per-call (layer-2) response timeout enforced here;
    this class keeps the codec synchronous-in-spirit (one request → one
    matching response), skipping any non-matching/notification frames.
    """

    def __init__(self, name: str, transport: "MCPTransport", rpc_timeout: float = 30.0) -> None:
        self.name = name
        self._transport = transport
        self.rpc_timeout = rpc_timeout
        self._next_id = 0
        self._opened = False

    async def _ensure_open(self) -> None:
        if not self._opened:
            await self._transport.open()
            self._opened = True

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self._ensure_open()
        self._next_id += 1
        req_id = self._next_id
        frame = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        await self._transport.send((json.dumps(frame) + "\n").encode("utf-8"))
        try:
            return await asyncio.wait_for(
                self._await_response(req_id),
                timeout=self.rpc_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MCPError(
                f"{self.name}: JSON-RPC request {method!r} timed out "
                f"after {self.rpc_timeout:g}s"
            ) from exc

    async def _await_response(self, req_id: int) -> Any:
        # Read frames until the one whose id matches our request. Notifications
        # and unrelated-id frames (e.g. server-side progress) are skipped.
        while True:
            raw = await self._transport.recv()
            try:
                msg = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MCPError(f"malformed JSON-RPC frame: {exc}") from exc
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != req_id:
                continue  # notification or mismatched response
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise MCPError(f"{self.name}: {message}")
            return msg.get("result")

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[MCPToolDef]:
        result = await self._request("tools/list")
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [
            MCPToolDef(
                name=str(t.get("name", "")),
                description=str(t.get("description", "")),
                input_schema=dict(t.get("inputSchema", {}) or {}),
            )
            for t in tools
            if isinstance(t, dict)
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._request("tools/call", {"name": name, "arguments": args})
        content = result.get("content", []) if isinstance(result, dict) else []
        return [c for c in content if isinstance(c, dict)]

    async def list_resources(self) -> list[MCPResourceDef]:
        result = await self._request("resources/list")
        resources = result.get("resources", []) if isinstance(result, dict) else []
        return [
            MCPResourceDef(
                uri=str(r.get("uri", "")),
                name=str(r.get("name", "")),
                description=str(r.get("description", "")),
                mime_type=str(r.get("mimeType", "")),
            )
            for r in resources
            if isinstance(r, dict)
        ]

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        result = await self._request("resources/read", {"uri": uri})
        contents = result.get("contents", []) if isinstance(result, dict) else []
        return [c for c in contents if isinstance(c, dict)]

    async def close(self) -> None:
        # Idempotent: always delegate to the transport (transports must tolerate
        # close-without-open). Avoids leaking a half-opened transport.
        self._opened = False
        await self._transport.close()


__all__ = [
    "CLIENT_INFO",
    "PROTOCOL_VERSION",
    "MCPError",
    "MCPResourceDef",
    "MCPSession",
    "MCPToolDef",
]
