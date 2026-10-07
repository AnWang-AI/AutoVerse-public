"""Real stdio JSON-RPC round-trip via StdioMcpClient against a tiny in-repo
echo MCP server (a ``python -c`` one-liner).

Hermetic: spawns the current interpreter, speaks newline-delimited JSON-RPC,
and skips gracefully if subprocess spawning is unavailable on the platform.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from autoverse.infra.mcp import MCPServerConfig
from autoverse.infra.mcp.transport import StdioMcpClient, default_client_factory

# A minimal MCP server: reads newline-delimited JSON-RPC requests on stdin and
# answers initialize / tools/list / tools/call on stdout. Newline-framed.
_ECHO_SERVER = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echo back",
                             "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        args = req.get("params", {}).get("arguments", {})
        result = {"content": [{"type": "text", "text": json.dumps(args)}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()
"""


async def _can_spawn() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_stdio_roundtrip_initialize_and_list_tools() -> None:
    if not await _can_spawn():
        pytest.skip("subprocess spawning unavailable on this platform")

    # Build the client directly from argv to avoid shell quoting fragility.
    client = StdioMcpClient("echo", [sys.executable, "-c", _ECHO_SERVER])
    try:
        info = await client.initialize()
        assert info["protocolVersion"] == "2024-11-05"
        defs = await client.list_tools()
        assert [d.name for d in defs] == ["echo"]
        blocks = await client.call_tool("echo", {"hello": "world"})
        assert blocks == [{"type": "text", "text": '{"hello": "world"}'}]
    finally:
        await client.close()


async def test_default_client_factory_builds_stdio_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    if not await _can_spawn():
        pytest.skip("subprocess spawning unavailable on this platform")

    # Write the echo server to a file so the command string splits cleanly with
    # shlex (the production default_client_factory path).
    script = tmp_path / "echo_server.py"
    script.write_text(_ECHO_SERVER, encoding="utf-8")
    cfg = MCPServerConfig("echo", command=f"{sys.executable} {script}")
    client = default_client_factory(cfg)
    try:
        info = await client.initialize()
        assert info["protocolVersion"] == "2024-11-05"
        defs = await client.list_tools()
        assert [d.name for d in defs] == ["echo"]
    finally:
        await client.close()


def test_default_client_factory_requires_command() -> None:
    with pytest.raises(ValueError):
        default_client_factory(MCPServerConfig("nocmd"))
