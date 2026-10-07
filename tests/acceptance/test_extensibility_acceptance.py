"""Phase 7 Workstream A — extensibility acceptance (docs/reference/testing.md §12, E1–E8).

Given/When/Then per row. MCP scenarios use an in-memory FAKE client (no network,
no MCP SDK). Hooks/skills/plugins exercise the injectable callable / descriptor
forms so the suite is deterministic and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.dispatcher import ToolDispatcher
from autoverse.core.tools.registry import ToolRegistry
from autoverse.core.tools.tracker import FileTracker
from autoverse.infra.hooks import HookDef, HookEngine, HookEvent, HookInput, HookOutcome
from autoverse.infra.mcp import (
    MCPManager,
    MCPServerConfig,
    MCPToolDef,
    ReconnectPolicy,
)
from autoverse.infra.permission.engine import PermissionEngine
from autoverse.infra.permission.matcher import RuleMatcher
from autoverse.infra.permission.policy import ToolMeta
from autoverse.infra.plugins import (
    ExtensionConflictError,
    Plugin,
    PluginManager,
    PluginManifest,
    detect_tool_name_conflicts,
)
from autoverse.infra.skills import Skill, SkillRegistry
from autoverse.types.abort import AbortController
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import Decision, ToolCall, ToolResult

from tests._doubles import MemStore


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeMCPClient:
    """In-memory MCP client double (no transport). Scriptable failure/replies."""

    def __init__(
        self,
        defs: list[MCPToolDef],
        *,
        replies: dict[str, list[dict[str, Any]]] | None = None,
        fail_until: int = 0,
    ) -> None:
        self._defs = defs
        self._replies = replies or {}
        self._fail_until = fail_until
        self.connects = 0
        self.closed = False

    async def initialize(self) -> dict[str, Any]:
        self.connects += 1
        if self.connects <= self._fail_until:
            raise ConnectionError(f"server down (attempt {self.connects})")
        return {"protocolVersion": "2024-11-05"}

    async def list_tools(self) -> list[MCPToolDef]:
        return list(self._defs)

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self._replies.get(name, [{"type": "text", "text": f"ran {name} {args}"}])

    async def close(self) -> None:
        self.closed = True


class _AllowAll:
    async def check(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.allow()

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _make_ctx(cwd: Path, perms: Any | None = None) -> ToolContext:
    sink = _RecordingSink()
    app = AppContext(
        config=Config(), store=MemStore(), cwd=cwd, model="m", events=sink
    )
    return ToolContext(
        cwd=cwd,
        permissions=perms or _AllowAll(),
        tracker=FileTracker(),
        signal=AbortController().signal,
        events=sink,
        app=app,
    )


# ── E1 — MCP tool injection ─────────────────────────────────────────────────
async def test_e1_mcp_tool_wrapped_and_dispatchable(tmp_path: Path) -> None:
    """Given an MCP server, When connected, Then its tool is wrapped as a Tool
    injected into the dispatcher and callable."""
    defn = MCPToolDef("search", "search issues", {"properties": {"q": {}}})
    client = FakeMCPClient([defn], replies={"search": [{"type": "text", "text": "hit-42"}]})
    mgr = MCPManager(
        {"linear": MCPServerConfig("linear")},
        client_factory=lambda cfg: client,
    )
    await mgr.setup()

    tools = mgr.contributed_tools()
    assert [t.name for t in tools] == ["mcp__linear__search"]

    registry = ToolRegistry.build(include=[])
    registry.register("mcp", tools)
    dispatcher = ToolDispatcher(registry)

    call = ToolCall(id="c1", name="mcp__linear__search", input={"q": "bug"})
    [result] = await dispatcher.dispatch([call], _make_ctx(tmp_path))
    assert not result.is_error
    assert "hit-42" in result.output
    assert result.metadata["server"] == "linear"
    await mgr.teardown()


# ── E2 — MCP fail-closed capability ──────────────────────────────────────────
async def test_e2_unknown_mcp_tool_fail_closed_process_then_ask(tmp_path: Path) -> None:
    """Given an unknown/undeclared-capability MCP tool, When permission is asked,
    Then it defaults to the most-restricted PROCESS tier and is asked (P7)."""
    defn = MCPToolDef("do", "arbitrary", {})
    mgr = MCPManager(
        {"sketchy": MCPServerConfig("sketchy")},  # not in any trust table
        client_factory=lambda cfg: FakeMCPClient([defn]),
    )
    await mgr.setup()
    [tool] = mgr.contributed_tools()
    # Fail-closed declaration: most restricted tier + conservative network assumption.
    assert tool.capability == CapabilityTier.PROCESS
    assert tool.needs_network is True

    # A declared (trusted) server can narrow the default — proves it is not hard-coded.
    trusted = MCPManager(
        {"trusted": MCPServerConfig("trusted", capability=CapabilityTier.READ_ONLY)},
        client_factory=lambda cfg: FakeMCPClient([defn]),
    )
    await trusted.setup()
    assert trusted.contributed_tools()[0].capability == CapabilityTier.READ_ONLY

    # P7: the permission engine ASKS for an unknown PROCESS-tier MCP tool. The
    # MCP layer only *declares* the tier (P2); the policy maps PROCESS->ask here.
    class _ProcessAsksMeta(ToolMeta):
        def is_irreversible(self, call: ToolCall) -> bool:
            return call.tier == CapabilityTier.PROCESS or super().is_irreversible(call)

    asked: list[str] = []

    class _AskingEngine(PermissionEngine):
        async def request_approval(self, call: ToolCall) -> Decision:
            asked.append(call.name)
            return Decision.deny("awaiting human approval", rule="ask")

    engine = _AskingEngine(
        RuleMatcher([]), global_mode="ask", meta_factory=_ProcessAsksMeta
    )
    call = ToolCall(id="c", name=tool.name, input={}, tier=tool.capability)
    decision = await engine.check(call)
    assert asked == [tool.name]
    assert decision.action == "deny"  # fail-closed: not auto-allowed
    await mgr.teardown()
    await trusted.teardown()


# ── E3 — MCP reconnect with exponential backoff ──────────────────────────────
async def test_e3_mcp_reconnect_exponential_backoff(tmp_path: Path) -> None:
    """Given a server that disconnects, When it comes back, Then the manager
    reconnects with exponential backoff under a two-layer timeout."""
    defn = MCPToolDef("ping", "ping", {})
    # Fails the first two connect attempts, succeeds on the third.
    client = FakeMCPClient([defn], fail_until=2)
    waited: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waited.append(delay)

    mgr = MCPManager(
        {"flaky": MCPServerConfig("flaky")},
        client_factory=lambda cfg: client,
        reconnect=ReconnectPolicy(base_delay=2.0, factor=2.0, max_attempts=5),
        sleep=fake_sleep,
    )
    await mgr.setup()  # first connect raises -> failed -> reconnect scheduled

    # Drive the reconnect loop to completion deterministically.
    task = mgr._reconnect_tasks["flaky"]  # type: ignore[attr-defined]
    await task

    # Exponential backoff delays recorded before each retry (2s, 4s, ...).
    assert waited[:2] == [2.0, 4.0]
    assert mgr.server_status["flaky"] == "connected"
    assert [t.name for t in mgr.contributed_tools()] == ["mcp__flaky__ping"]
    await mgr.teardown()


# ── E4 — plugin loading + disabled switch ────────────────────────────────────
async def test_e4_plugin_discovery_and_disabled_switch(tmp_path: Path) -> None:
    """Given a plugin providing tools/commands/hooks, When discovered, Then it
    loads via entry-point/dir scan; plugins.disabled can disable it (E4)."""
    from autoverse.core.tools.builtin.think import ThinkTool

    def make_plugin(name: str) -> Plugin:
        return Plugin(
            manifest=PluginManifest(name=name, provides=frozenset({"tools", "hooks"})),
            root=tmp_path,
            tools_factory=lambda: [ThinkTool()],
            hooks=[HookDef(HookEvent.PRE_TOOL_USE, _noop_hook, matcher="*")],
        )

    # entry-point channel yields two plugins; one is disabled.
    mgr = PluginManager(
        disabled={"beta"},
        entry_point_loader=lambda: [make_plugin("alpha"), make_plugin("beta")],
    )
    await mgr.setup()

    statuses = mgr.plugin_status
    assert statuses["alpha"].state == "loaded"
    assert statuses["beta"].state == "disabled"
    # Only the enabled plugin's tool/hook were collected.
    assert [t.name for t in mgr.contributed_tools()] == ["think"]
    assert len(mgr.contributed_hooks()) == 1


async def _noop_hook(_: HookInput) -> HookOutcome:
    return HookOutcome()


# ── E5 — PreToolUse hook can deny (middleware semantics) ─────────────────────
async def test_e5_pretooluse_hook_denies_via_middleware(tmp_path: Path) -> None:
    """Given a PreToolUse hook that denies, When a matching tool is called, Then
    the hook blocks the call (middleware semantics)."""
    post_ran: list[str] = []

    async def deny_env_writes(inp: HookInput) -> HookOutcome:
        assert inp.tool is not None
        if inp.tool.name == "write_file":
            return HookOutcome(decision="deny", reason="writing .env is forbidden")
        return HookOutcome()

    async def observe(inp: HookInput) -> HookOutcome:
        post_ran.append(inp.tool.name if inp.tool else "?")
        return HookOutcome()

    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, deny_env_writes, matcher="write_file"))
    engine.add(HookDef(HookEvent.POST_TOOL_USE, observe, blocking=False))
    mw = engine.middleware()

    inner_calls: list[str] = []

    async def inner(call: ToolCall, ctx: Any) -> ToolResult:
        inner_calls.append(call.name)
        return ToolResult.ok("done")

    blocked = await mw(ToolCall(id="1", name="write_file", input={}), None, inner)
    assert blocked.is_error
    assert blocked.error_kind == "hook_blocked"
    assert "forbidden" in blocked.output
    assert inner_calls == []   # call never reached execution
    assert post_ran == []      # post-hook not run for a denied call

    allowed = await mw(ToolCall(id="2", name="read_file", input={}), None, inner)
    assert not allowed.is_error
    assert inner_calls == ["read_file"]
    assert post_ran == ["read_file"]  # post-hook observed the allowed call


# ── E6 — slow hook times out fail-closed ─────────────────────────────────────
async def test_e6_slow_hook_times_out_fail_closed(tmp_path: Path) -> None:
    """Given a slow hook, When it exceeds its timeout, Then it fails closed (deny)
    without hanging the main loop."""
    import asyncio

    async def slow_hook(_: HookInput) -> HookOutcome:
        await asyncio.sleep(3600)
        return HookOutcome()

    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, slow_hook, timeout=0.05))

    outcome = await asyncio.wait_for(
        engine.run(HookEvent.PRE_TOOL_USE, tool=ToolCall(id="1", name="bash", input={})),
        timeout=2.0,  # the engine must return well before this — it never hangs
    )
    assert outcome.decision == "deny"
    assert "fail-closed" in outcome.reason

    # And via the middleware the slow PreToolUse hook blocks the call.
    mw = engine.middleware()

    async def inner(call: ToolCall, ctx: Any) -> ToolResult:
        return ToolResult.ok("ran")

    result = await mw(ToolCall(id="2", name="bash", input={}), None, inner)
    assert result.is_error and result.error_kind == "hook_blocked"


# ── E7 — skill matches description; Available Skills derived from registry ──
async def test_e7_skill_match_and_available_section_derived(tmp_path: Path) -> None:
    """Given a user request matching a skill description, Then that skill is
    triggered; the Available Skills section is derived from the registry (P1)."""
    reg = SkillRegistry()
    reg.register(
        Skill(
            name="simplify",
            description="Review and simplify recently changed code when the user says 'simplify this'.",
            body="Simplify the following.",
        )
    )
    reg.register(
        Skill(
            name="paper-outline",
            description="Generate a paper outline when the user wants to start writing a paper outline.",
        )
    )

    matched = reg.match("can you simplify this function please")
    assert [s.name for s in matched] == ["simplify"]
    # Non-matching request triggers nothing.
    assert reg.match("what's the weather") == []

    # P1: Available Skills section is derived from the registry — both skills.
    section = reg.available_section()
    assert "## Available Skills" in section
    assert "**simplify**" in section and "**paper-outline**" in section
    assert "When to use" not in section

    # Single source of truth: removing a skill removes it from the derived section.
    reg2 = SkillRegistry()
    reg2.register(reg.get("simplify"))  # type: ignore[arg-type]
    section2 = reg2.available_section()
    assert "**simplify**" in section2 and "**paper-outline**" not in section2


# ── E8 — name conflict detected, no silent overwrite ─────────────────────────
async def test_e8_tool_name_conflict_raises(tmp_path: Path) -> None:
    """Given two extensions providing the same tool name, When loaded, Then the
    conflict is detected and raises — never a silent overwrite (§9.4)."""
    from autoverse.core.tools.builtin.think import ThinkTool

    def plugin(name: str) -> Plugin:
        return Plugin(
            manifest=PluginManifest(name=name, provides=frozenset({"tools"})),
            root=tmp_path,
            tools_factory=lambda: [ThinkTool()],  # both export tool name "think"
        )

    mgr = PluginManager(
        entry_point_loader=lambda: [plugin("p1"), plugin("p2")],
    )
    with pytest.raises(ExtensionConflictError, match="duplicate tool name 'think'"):
        await mgr.setup()

    # The standalone detector raises on the same condition.
    with pytest.raises(ExtensionConflictError):
        detect_tool_name_conflicts([ThinkTool(), ThinkTool()])

    # The ToolRegistry itself also refuses a silent overwrite (defence in depth).
    registry = ToolRegistry.build(include=[])
    with pytest.raises(ValueError, match="Duplicate tool name"):
        registry.register("x", [ThinkTool(), ThinkTool()])
