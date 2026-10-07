"""gap9 acceptance — the built engine wires MCP / Skills / Hooks.

Proves production wiring (gap9, A3): EngineBuilder.build() constructs the extension
sources, runs their lifecycle, registers contributed tools into the SAME registry the
model sees (P1), projects their fragments into the system prompt (P8), and threads the
HookEngine PreToolUse middleware before permission/execution. The DI seam
``EngineOptions.extension_sources`` injects in-memory sources for determinism.
See docs/reference/extensions.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.infra.hooks.engine import HookDef, HookEngine, HookEvent, HookInput, HookOutcome
from autoverse.infra.mcp import MCPToolDef
from autoverse.infra.mcp.manager import MCPManager
from autoverse.infra.mcp.manager import MCPServerConfig as MCPRuntimeConfig
from autoverse.infra.permission.fixed import AllowAllPermissionAdapter
from autoverse.infra.skills.registry import Skill, SkillRegistry
from autoverse.types import (
    TextDeltaPayload,
    ToolStartPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.types.messages import Message, ToolResultBlock

from tests._doubles import ScriptedProvider


class _FakeMcpClient:
    """Minimal MCP client: echoes the called tool name + args (test seam, §4.7)."""

    def __init__(self, defs: list[MCPToolDef]) -> None:
        self._defs = defs

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self) -> list[MCPToolDef]:
        return list(self._defs)

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "text", "text": f"{name}:{sorted(args.items())}"}]

    async def close(self) -> None:
        return None


def _tool_turn(tid: str, name: str, inp: dict[str, Any]) -> list[Any]:
    return [
        make_event(ToolStartPayload(tool_use_id=tid, tool_name=name, tool_input=inp)),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
    ]


def _text_turn(text: str) -> list[Any]:
    return [
        make_event(TextDeltaPayload(text=text)),
        make_event(UsagePayload(usage=Usage(input_tokens=1, output_tokens=1), model="m")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]


async def _drain(engine: Any, prompt: str) -> None:
    async for _ in engine.run(prompt):
        pass


def _last_tool_result(conv: Any) -> ToolResultBlock:
    for msg in reversed(conv.view().messages):
        if isinstance(msg, Message) and msg.role == "user":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    return block
    raise AssertionError("no tool-result block found")


# ── gap9-A3: an MCP tool reaches LLMRequest.tools and really executes ─────────
async def test_mcp_tool_registered_and_callable(tmp_path: Path) -> None:
    mcp = MCPManager(
        {"x": MCPRuntimeConfig("x")},
        client_factory=lambda cfg: _FakeMcpClient([MCPToolDef("op", "", {})]),
    )
    provider = ScriptedProvider([_tool_turn("m1", "mcp__x__op", {}), _text_turn("done")])
    engine = await EngineBuilder(
        EngineOptions(
            model="echo", cwd=tmp_path, llm_provider=provider, extension_sources=(mcp,),
            permission_engine=AllowAllPermissionAdapter(),
        )
    ).build()
    await _drain(engine, "use the mcp tool")

    names = {t.name for t in provider.requests[0].tools}
    assert "mcp__x__op" in names  # contributed tool in the model-facing schema (P1)
    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "op:" in block.content  # real MCP round-trip, not an echo


# ── gap9-A3: MCP tools project into the system prompt (P8) ────────────────────
async def test_mcp_tools_in_prompt(tmp_path: Path) -> None:
    mcp = MCPManager(
        {"x": MCPRuntimeConfig("x")},
        client_factory=lambda cfg: _FakeMcpClient([MCPToolDef("op", "the op", {})]),
    )
    provider = ScriptedProvider([_text_turn("done")])
    engine = await EngineBuilder(
        EngineOptions(
            model="echo", cwd=tmp_path, llm_provider=provider, extension_sources=(mcp,)
        )
    ).build()
    await _drain(engine, "hello")
    system = provider.requests[0].system
    assert "Connected MCP Tools" in system
    assert "mcp__x__op" in system


# ── gap9-A3: a blocking PreToolUse hook intercepts and is announced in prompt ─
async def test_blocking_hook_intercepts_and_projects(tmp_path: Path) -> None:
    async def _deny(_hi: HookInput) -> HookOutcome:
        return HookOutcome(decision="deny", reason="read_file is forbidden in this repo")

    hooks = HookEngine(
        {
            HookEvent.PRE_TOOL_USE: [
                HookDef(
                    event=HookEvent.PRE_TOOL_USE,
                    handler=_deny,
                    matcher="read_file",
                    blocking=True,
                    description="read_file is forbidden in this repo",
                )
            ]
        }
    )
    provider = ScriptedProvider(
        [_tool_turn("r1", "read_file", {"file_path": "x"}), _text_turn("done")]
    )
    engine = await EngineBuilder(
        EngineOptions(
            model="echo", cwd=tmp_path, llm_provider=provider, extension_sources=(hooks,)
        )
    ).build()
    await _drain(engine, "read the file")

    # P8: the constraint is announced so the model can adjust.
    system = provider.requests[0].system
    assert "Active Tool Hooks" in system
    assert "read_file is forbidden" in system
    # §6.4: the hook denies before permission/execution; feedback reaches the model.
    block = _last_tool_result(engine._conversation)
    assert block.is_error
    assert "Blocked by hook" in block.content


# ── gap9-A3: skills project into the prompt and expose the Skill tool ─────────
async def test_skills_registered_and_in_prompt(tmp_path: Path) -> None:
    skills = SkillRegistry(cwd=tmp_path)
    skills.register(
        Skill(
            name="simplify",
            description="Clean up the code you just wrote when the user says 'simplify this'.",
        )
    )
    provider = ScriptedProvider([_text_turn("done")])
    engine = await EngineBuilder(
        EngineOptions(
            model="echo", cwd=tmp_path, llm_provider=provider, extension_sources=(skills,)
        )
    ).build()
    await _drain(engine, "hello")

    system = provider.requests[0].system
    assert "Available Skills" in system
    assert "simplify" in system
    names = {t.name for t in provider.requests[0].tools}
    assert "Skill" in names
