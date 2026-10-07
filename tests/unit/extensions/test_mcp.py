"""Unit tests for MCPManager / MCPTool (docs/reference/mcp.md)."""

from __future__ import annotations

from typing import Any

import pytest

from autoverse.infra.mcp import (
    MCP_DEFAULT_TIER,
    MCPManager,
    MCPServerConfig,
    MCPTool,
    MCPToolDef,
    ReconnectPolicy,
    build_args_model,
    mcp_tool_name,
    tier_for_server,
)
from autoverse.types.capability import CapabilityTier


class FakeClient:
    def __init__(self, defs: list[MCPToolDef], *, fail_until: int = 0) -> None:
        self._defs = defs
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
        return [{"type": "text", "text": f"{name}:{sorted(args.items())}"}]

    async def close(self) -> None:
        return None


def test_tool_name_convention() -> None:
    assert mcp_tool_name("linear", "search") == "mcp__linear__search"


def test_tier_for_server_unknown_is_process() -> None:
    assert tier_for_server("unknown", None) == CapabilityTier.PROCESS
    assert tier_for_server("unknown", {}) == CapabilityTier.PROCESS
    assert MCP_DEFAULT_TIER == CapabilityTier.PROCESS


def test_tier_for_server_table_override() -> None:
    table = {"trusted": CapabilityTier.READ_ONLY}
    assert tier_for_server("trusted", table) == CapabilityTier.READ_ONLY
    assert tier_for_server("other", table) == CapabilityTier.PROCESS


def test_build_args_model_permissive_passthrough() -> None:
    model = build_args_model("t", {"properties": {"q": {}}, "required": ["q"]})
    inst = model.model_validate({"q": "hi", "page": 2})
    assert inst.model_dump() == {"q": "hi", "page": 2}
    with pytest.raises(Exception):
        model.model_validate({})  # required q missing


async def test_unknown_server_tool_fail_closed_process() -> None:
    mgr = MCPManager(
        {"x": MCPServerConfig("x")},
        client_factory=lambda cfg: FakeClient([MCPToolDef("op", "", {})]),
    )
    await mgr.setup()
    [tool] = mgr.contributed_tools()
    assert isinstance(tool, MCPTool)
    assert tool.capability == CapabilityTier.PROCESS
    assert tool.needs_network is True


async def test_declared_capability_narrows_default() -> None:
    mgr = MCPManager(
        {"ro": MCPServerConfig("ro", capability=CapabilityTier.READ_ONLY)},
        client_factory=lambda cfg: FakeClient([MCPToolDef("op", "", {})]),
    )
    await mgr.setup()
    assert mgr.contributed_tools()[0].capability == CapabilityTier.READ_ONLY


async def test_setup_isolates_failed_server() -> None:
    waited: list[float] = []

    async def sleep(d: float) -> None:
        waited.append(d)

    mgr = MCPManager(
        {
            "ok": MCPServerConfig("ok"),
            "bad": MCPServerConfig("bad"),
        },
        client_factory=lambda cfg: FakeClient(
            [MCPToolDef("t", "", {})], fail_until=99 if cfg.name == "bad" else 0
        ),
        reconnect=ReconnectPolicy(max_attempts=2),
        sleep=sleep,
    )
    await mgr.setup()
    # Failed server isolated; healthy one still contributes its tool.
    assert mgr.server_status["ok"] == "connected"
    assert [t.name for t in mgr.contributed_tools()] == ["mcp__ok__t"]
    # The bad server's reconnect task exhausts attempts (deterministic sleep).
    await mgr._reconnect_tasks["bad"]  # type: ignore[attr-defined]
    assert mgr.server_status["bad"] == "failed"
    await mgr.teardown()


def test_reconnect_policy_backoff_curve() -> None:
    pol = ReconnectPolicy(base_delay=2.0, factor=2.0, max_delay=60.0)
    assert [pol.delay_for(n) for n in range(1, 7)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


async def test_prompt_fragment_lists_connected_tools() -> None:
    mgr = MCPManager(
        {"srv": MCPServerConfig("srv")},
        client_factory=lambda cfg: FakeClient([MCPToolDef("look", "inspect", {})]),
    )
    await mgr.setup()
    frag = mgr.prompt_fragment()
    assert "## Connected MCP Tools" in frag
    assert "mcp__srv__look" in frag
    # Empty manager projects nothing (P8 boundary).
    assert MCPManager({}, client_factory=lambda cfg: FakeClient([])).prompt_fragment() == ""
