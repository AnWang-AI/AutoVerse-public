"""Unit tests for PluginManager (docs/reference/extensions.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.core.tools.builtin.think import ThinkTool
from autoverse.infra.hooks import HookDef, HookEvent, HookInput, HookOutcome
from autoverse.infra.plugins import (
    ExtensionConflictError,
    Plugin,
    PluginManager,
    PluginManifest,
    detect_tool_name_conflicts,
)
from autoverse.infra.skills import Skill


async def _noop(_: HookInput) -> HookOutcome:
    return HookOutcome()


def _plugin(name: str, provides: set[str], root: Path, **kw: object) -> Plugin:
    return Plugin(manifest=PluginManifest(name=name, provides=frozenset(provides)), root=root, **kw)  # type: ignore[arg-type]


async def test_disabled_filtered_at_discovery(tmp_path: Path) -> None:
    mgr = PluginManager(
        disabled={"off"},
        entry_point_loader=lambda: [
            _plugin("on", {"tools"}, tmp_path, tools_factory=lambda: [ThinkTool()]),
            _plugin("off", {"tools"}, tmp_path, tools_factory=lambda: [ThinkTool()]),
        ],
    )
    await mgr.setup()
    assert mgr.plugin_status["on"].state == "loaded"
    assert mgr.plugin_status["off"].state == "disabled"
    assert [t.name for t in mgr.contributed_tools()] == ["think"]


async def test_only_declared_categories_activated(tmp_path: Path) -> None:
    # Provides only "skills" — its tools_factory must NOT be invoked (§5.6, P4).
    called: list[str] = []

    def factory() -> list:
        called.append("tools")
        return [ThinkTool()]

    mgr = PluginManager(
        entry_point_loader=lambda: [
            _plugin(
                "skillsonly",
                {"skills"},
                tmp_path,
                tools_factory=factory,
                skills=[Skill(name="s1")],
            )
        ]
    )
    await mgr.setup()
    assert called == []  # tools not declared -> not instantiated
    assert mgr.contributed_tools() == []
    assert "s1" in [s.name for s in mgr.contributed_skills()["skillsonly"]]


async def test_broken_plugin_isolated(tmp_path: Path) -> None:
    def boom() -> list:
        raise RuntimeError("bad tool module")

    mgr = PluginManager(
        entry_point_loader=lambda: [
            _plugin("good", {"tools"}, tmp_path, tools_factory=lambda: [ThinkTool()]),
            _plugin("bad", {"tools"}, tmp_path, tools_factory=boom),
        ]
    )
    await mgr.setup()
    assert mgr.plugin_status["good"].state == "loaded"
    assert mgr.plugin_status["bad"].state == "failed"
    assert "bad tool module" in mgr.plugin_status["bad"].reason
    assert [t.name for t in mgr.contributed_tools()] == ["think"]


async def test_unsupported_api_version_rejected(tmp_path: Path) -> None:
    mgr = PluginManager(
        entry_point_loader=lambda: [
            Plugin(
                manifest=PluginManifest(name="future", api_version="99", provides=frozenset({"tools"})),
                root=tmp_path,
                tools_factory=lambda: [ThinkTool()],
            )
        ]
    )
    await mgr.setup()
    assert mgr.plugin_status["future"].state == "failed"
    assert "not in supported range" in mgr.plugin_status["future"].reason
    assert mgr.contributed_tools() == []


async def test_tool_name_conflict_raises(tmp_path: Path) -> None:
    mgr = PluginManager(
        entry_point_loader=lambda: [
            _plugin("p1", {"tools"}, tmp_path, tools_factory=lambda: [ThinkTool()]),
            _plugin("p2", {"tools"}, tmp_path, tools_factory=lambda: [ThinkTool()]),
        ]
    )
    with pytest.raises(ExtensionConflictError, match="duplicate tool name 'think'"):
        await mgr.setup()


def test_detect_tool_name_conflicts_helper() -> None:
    detect_tool_name_conflicts([ThinkTool()])  # no raise for distinct names
    with pytest.raises(ExtensionConflictError):
        detect_tool_name_conflicts([ThinkTool(), ThinkTool()])


async def test_contributed_hooks_and_commands(tmp_path: Path) -> None:
    hook = HookDef(HookEvent.PRE_TOOL_USE, _noop)
    mgr = PluginManager(
        entry_point_loader=lambda: [
            _plugin(
                "p",
                {"hooks", "commands"},
                tmp_path,
                hooks=[hook],
                commands=["/dothing"],
            )
        ]
    )
    await mgr.setup()
    assert mgr.contributed_hooks() == [hook]
    assert mgr.contributed_commands() == ["/dothing"]
    assert mgr.prompt_fragment() == ""  # plugins project via their sub-sources


def test_from_config_reads_defensively(tmp_path: Path) -> None:
    # Config has no `plugins` section yet (Workstream D owns it); must not crash.
    from autoverse.app.config import Config

    mgr = PluginManager.from_config(Config(), cwd=tmp_path, entry_point_loader=lambda: [])
    assert isinstance(mgr, PluginManager)
