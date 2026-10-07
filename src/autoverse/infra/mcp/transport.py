"""MCP transport layer — byte I/O only, orthogonal to the codec (P6).

``MCPTransport`` is the narrow seam the :class:`~autoverse.infra.mcp.session.
MCPSession` codec depends on: it just moves frames in and out. ``StdioTransport``
spawns an MCP server via ``asyncio.create_subprocess_exec`` and speaks
newline-delimited JSON over stdin/stdout, draining stderr in the background so
a chatty server cannot deadlock the pipe (§4.3 note). A WebSocket / HTTP
transport would implement the same Protocol with zero codec changes (§4.8).

``StdioMcpClient`` is the concrete ``MCPClient`` (the manager seam): it owns a
``StdioTransport`` + ``MCPSession`` and exposes initialize/list_tools/call_tool/
list_resources/read_resource/close. ``default_client_factory`` builds one from
an :class:`~autoverse.infra.mcp.manager.MCPServerConfig` so the manager works
without an injected factory in production while tests keep injecting fakes.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from autoverse.infra.mcp.session import MCPResourceDef, MCPSession, MCPToolDef

if TYPE_CHECKING:
    from autoverse.infra.mcp.manager import MCPClient, MCPServerConfig


@runtime_checkable
class MCPTransport(Protocol):
    """Transport seam: only moves frames; knows nothing of JSON-RPC (P6)."""

    async def open(self) -> None: ...

    async def send(self, frame: bytes) -> None: ...

    async def recv(self) -> bytes: ...  # stdio: one line; http: one response

    async def close(self) -> None: ...


class StdioTransport:
    """Subprocess stdio transport: newline-delimited frames over stdin/stdout.

    stderr is drained by a background task so a verbose server cannot fill the
    OS pipe buffer and deadlock (§4.3).
    """

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
        except Exception:  # noqa: BLE001 — draining must never raise into caller
            return

    async def send(self, frame: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("stdio transport not open")
        proc.stdin.write(frame)
        await proc.stdin.drain()

    async def recv(self) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise RuntimeError("stdio transport not open")
        line = await proc.stdout.readline()
        if not line:
            raise EOFError("MCP server closed stdout")
        return line

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None


class StdioMcpClient:
    """Concrete ``MCPClient`` backed by ``StdioTransport`` + ``MCPSession``."""

    def __init__(self, name: str, argv: list[str], *, rpc_timeout: float = 30.0) -> None:
        self._transport = StdioTransport(argv)
        self._session = MCPSession(name, self._transport, rpc_timeout=rpc_timeout)

    async def initialize(self) -> dict[str, Any]:
        return await self._session.initialize()

    async def list_tools(self) -> list[MCPToolDef]:
        return await self._session.list_tools()

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._session.call_tool(name, args)

    async def list_resources(self) -> list[MCPResourceDef]:
        return await self._session.list_resources()

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        return await self._session.read_resource(uri)

    async def close(self) -> None:
        await self._session.close()


def default_client_factory(cfg: "MCPServerConfig") -> "MCPClient":
    """Build a real stdio ``MCPClient`` from a server config's ``command``.

    The command string is split with ``shlex`` into argv. Used by ``MCPManager``
    when no factory is injected (production path); tests inject fakes instead.
    """
    if not cfg.command:
        raise ValueError(
            f"MCP server {cfg.name!r} has no command for the default stdio client"
        )
    argv = shlex.split(cfg.command)
    return StdioMcpClient(cfg.name, argv, rpc_timeout=cfg.rpc_timeout)


__all__ = [
    "MCPTransport",
    "StdioMcpClient",
    "StdioTransport",
    "default_client_factory",
]
