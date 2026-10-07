"""Resource-tool dispatch, reconnect TOOL_LIST_CHANGED signal, and the
default stdio client factory (docs/reference/mcp.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoverse.app.context import AppContext
from autoverse.core.tools.context import ToolContext
from autoverse.infra.mcp import (
    ListMcpResourcesTool,
    MCPManager,
    MCPResourceDef,
    MCPServerConfig,
    MCPToolDef,
    ReadMcpResourceTool,
    ReconnectPolicy,
)
from autoverse.app.config import Config
from autoverse.types.abort import AbortController
from autoverse.types.capability import CapabilityTier


class FakeClientWithResources:
    def __init__(
        self,
        defs: list[MCPToolDef],
        resources: list[MCPResourceDef] | None = None,
        *,
        fail_until: int = 0,
    ) -> None:
        self._defs = defs
        self._resources = resources or []
        self._fail_until = fail_until
        self.connects = 0

    async def initialize(self) -> dict[str, Any]:
        self.connects += 1
        if self.connects <= self._fail_until:
            raise ConnectionError("down")
        return {}

    async def list_tools(self) -> list[MCPToolDef]:
        return list(self._defs)

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "text", "text": name}]

    async def list_resources(self) -> list[MCPResourceDef]:
        return list(self._resources)

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        return [{"uri": uri, "text": f"body-of-{uri}"}]

    async def close(self) -> None:
        return None


class LegacyFakeClient:
    """No resource methods — proves the manager tolerates their absence."""

    def __init__(self, defs: list[MCPToolDef]) -> None:
        self._defs = defs

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self) -> list[MCPToolDef]:
        return list(self._defs)

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "text", "text": name}]

    async def close(self) -> None:
        return None


class _AllowAll:
    async def check(self, call: Any) -> Any: ...
    def check_sync(self, call: Any) -> Any: ...
    async def request_approval(self, call: Any) -> Any: ...
    def cancel_all(self) -> None: ...


def _ctx(tmp_path: Path) -> ToolContext:
    class _Sink:
        async def emit(self, event: Any) -> None: ...

    sink = _Sink()
    app = AppContext(config=Config(), store=None, cwd=tmp_path, model="m", events=sink)
    return ToolContext(
        cwd=tmp_path,
        permissions=_AllowAll(),
        tracker=None,  # type: ignore[arg-type]
        signal=AbortController().signal,
        events=sink,
        app=app,
    )


def test_resource_defn_frozen() -> None:
    d = MCPResourceDef("file:///x", "X", "desc", "text/plain")
    assert d.uri == "file:///x"
    with pytest.raises(Exception):
        d.uri = "y"  # type: ignore[misc]


async def test_resource_tools_exposed_when_connected(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources(
            [MCPToolDef("t")],
            [MCPResourceDef("file:///a", "A", "first", "text/plain")],
        ),
    )
    await mgr.setup()
    # Per-server tools stay in contributed_tools (exact set, P1).
    assert [t.name for t in mgr.contributed_tools()] == ["mcp__s__t"]
    # Resource tools are exposed separately, only when a server is connected.
    res_names = {t.name for t in mgr.resource_tools()}
    assert res_names == {"list_mcp_resources", "read_mcp_resource"}


async def test_resource_tools_absent_when_no_server() -> None:
    mgr = MCPManager({}, client_factory=lambda cfg: FakeClientWithResources([]))
    await mgr.setup()
    assert mgr.contributed_tools() == []
    assert mgr.resource_tools() == []


async def test_resource_tools_are_read_only_tier(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources([MCPToolDef("t")]),
    )
    await mgr.setup()
    tools = {t.name: t for t in mgr.resource_tools()}
    assert tools["list_mcp_resources"].capability == CapabilityTier.READ_ONLY
    assert tools["read_mcp_resource"].capability == CapabilityTier.READ_ONLY


async def test_list_mcp_resources_dispatch(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources(
            [MCPToolDef("t")],
            [MCPResourceDef("file:///a", "A", "first", "text/plain")],
        ),
    )
    await mgr.setup()
    tool = ListMcpResourcesTool(mgr)
    res = await tool.run(tool.Args(), _ctx(tmp_path))
    assert not res.is_error
    assert "file:///a" in res.output
    assert "s" in res.output


async def test_read_mcp_resource_dispatch(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources([MCPToolDef("t")]),
    )
    await mgr.setup()
    tool = ReadMcpResourceTool(mgr)
    res = await tool.run(tool.Args(uri="file:///doc"), _ctx(tmp_path))
    assert not res.is_error
    assert "body-of-file:///doc" in res.output


async def test_read_mcp_resource_unknown_uri_errors(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources([MCPToolDef("t")]),
    )
    await mgr.setup()
    tool = ReadMcpResourceTool(mgr)
    res = await tool.run(tool.Args(uri="file:///doc", server="ghost"), _ctx(tmp_path))
    assert res.is_error


async def test_manager_tolerates_client_without_resource_methods(tmp_path: Path) -> None:
    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: LegacyFakeClient([MCPToolDef("t")]),
    )
    await mgr.setup()
    tool = ListMcpResourcesTool(mgr)
    res = await tool.run(tool.Args(), _ctx(tmp_path))
    # Treated as empty resources rather than crashing.
    assert not res.is_error


async def test_reconnect_increments_tool_list_changed(tmp_path: Path) -> None:
    waited: list[float] = []

    async def sleep(d: float) -> None:
        waited.append(d)

    client = FakeClientWithResources([MCPToolDef("ping")], fail_until=1)
    mgr = MCPManager(
        {"flaky": MCPServerConfig("flaky")},
        client_factory=lambda cfg: client,
        reconnect=ReconnectPolicy(max_attempts=3),
        sleep=sleep,
    )
    await mgr.setup()
    # Initial connect failed; a backoff reconnect task was scheduled.
    await mgr._reconnect_tasks["flaky"]  # type: ignore[attr-defined]
    assert mgr.server_status["flaky"] == "connected"
    # Exactly one successful reconnect -> one TOOL_LIST_CHANGED signal.
    assert mgr.tool_list_changed_count == 1
    await mgr.teardown()


async def test_trigger_reconnect_increments_signal(tmp_path: Path) -> None:
    async def sleep(d: float) -> None: ...

    mgr = MCPManager(
        {"s": MCPServerConfig("s")},
        client_factory=lambda cfg: FakeClientWithResources([MCPToolDef("t")]),
        reconnect=ReconnectPolicy(max_attempts=2),
        sleep=sleep,
    )
    await mgr.setup()
    assert mgr.tool_list_changed_count == 0
    await mgr.trigger_reconnect("s")
    assert mgr.tool_list_changed_count == 1
    await mgr.teardown()
