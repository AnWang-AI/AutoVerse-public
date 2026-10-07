"""Main-agent system prompt subsystem tests (docs/architecture/runtime-flow.md §12)."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.app.prompt_env import build_environment
from autoverse.core.prompt import PROMPT_SECTIONS, PromptAssembler, build_tool_list
from autoverse.core.subagent.definition import AGENT_DEFINITIONS


# ── sections (static content) ─────────────────────────────────────────────────
def test_all_sections_present_and_nonempty() -> None:
    for f in fields(PROMPT_SECTIONS):
        value = getattr(PROMPT_SECTIONS, f.name)
        assert isinstance(value, str) and value.strip(), f"section {f.name} empty"


def test_intro_states_safety_red_lines() -> None:
    intro = PROMPT_SECTIONS.intro
    assert "AutoVerse" in intro
    assert "拒绝" in intro  # malicious-use refusal
    assert "URL" in intro


def test_tool_format_section_forbids_bare_json() -> None:
    using = PROMPT_SECTIONS.using_tools
    assert "JSON" in using
    assert "绝对路径" in using  # absolute-path mandate for bash


# ── assembler (render order) ──────────────────────────────────────────────────
def test_render_section_order_and_skips_empty() -> None:
    asm = PromptAssembler()
    out = asm.render()
    # Static spine always present.
    assert PROMPT_SECTIONS.intro in out
    assert PROMPT_SECTIONS.subagent_workflow in out
    # Empty dynamic sections are skipped (no stray headers).
    assert "## 项目记忆" not in out
    assert "## 附加指令" not in out
    # Order: intro before subagent workflow before system behavior.
    assert out.index(PROMPT_SECTIONS.intro) < out.index(PROMPT_SECTIONS.subagent_workflow)
    assert out.index(PROMPT_SECTIONS.subagent_workflow) < out.index(
        PROMPT_SECTIONS.system_behavior
    )


def test_render_includes_dynamic_sections_when_set() -> None:
    asm = PromptAssembler()
    asm.environment = "## 环境\n- 工作目录: /tmp"
    asm.memory = "global memory block"
    asm.append = "obey the project lint rules"
    out = asm.render()
    assert "## 环境" in out
    assert "## 项目记忆\n\nglobal memory block" in out
    assert "## 附加指令\n\nobey the project lint rules" in out
    # memory comes before append (append is the last, highest-priority section).
    assert out.index("## 项目记忆") < out.index("## 附加指令")


def test_session_guidance_variants() -> None:
    interactive = PromptAssembler(session_mode="interactive").render()
    one_shot = PromptAssembler(session_mode="one_shot").render()
    assert "交互式" in interactive
    assert "不要反问" in one_shot
    assert "不要反问" not in interactive


# ── extension fragments (P8 projection) ───────────────────────────────────────
def test_add_fragment_ignores_empty_and_overwrites_same_name() -> None:
    asm = PromptAssembler()
    asm.add_fragment("mcp", "")  # ignored
    asm.add_fragment("mcp", "   ")  # ignored
    assert "mcp" not in asm._fragments
    asm.add_fragment("mcp", "## Connected MCP Tools\n- foo")
    asm.add_fragment("mcp", "## Connected MCP Tools\n- bar")  # overwrite
    out = asm.render()
    assert "- bar" in out and "- foo" not in out


def test_fragments_render_in_stable_name_order() -> None:
    asm = PromptAssembler()
    asm.add_fragment("zeta", "ZETA-FRAG")
    asm.add_fragment("alpha", "ALPHA-FRAG")
    out = asm.render()
    assert out.index("ALPHA-FRAG") < out.index("ZETA-FRAG")
    # Fragments sit after subagent workflow, before session guidance.
    assert out.index(PROMPT_SECTIONS.subagent_workflow) < out.index("ALPHA-FRAG")


# ── tool list derivation (P1) ─────────────────────────────────────────────────
class _StubRegistry:
    def __init__(self, schemas: list[dict]) -> None:
        self._schemas = schemas

    def schemas(self) -> list[dict]:
        return self._schemas


def test_build_tool_list_from_registry_derives_one_line_each() -> None:
    reg = _StubRegistry(
        [
            {"name": "read_file", "description": "读取文件内容。还有更多。"},
            {"name": "bash", "description": "Run a shell command. More detail."},
        ]
    )
    out = build_tool_list(reg)
    assert "- read_file: 读取文件内容" in out
    assert "- bash: Run a shell command" in out
    # Only the first sentence is kept.
    assert "还有更多" not in out
    assert "More detail" not in out


def test_build_tool_list_falls_back_without_registry() -> None:
    assert "## 可用工具" in build_tool_list(None)
    assert "## 可用工具" in build_tool_list(_StubRegistry([]))


# ── environment (app layer, P5) ───────────────────────────────────────────────
def test_build_environment_reports_cwd_date_platform(tmp_path: Path) -> None:
    env = build_environment(tmp_path)
    assert str(tmp_path) in env
    assert "今天的日期" in env
    assert "平台" in env
    assert "是否 git 仓库" in env


# ── alignment with subagent role definitions (P1, anti-drift) ─────────────────
def test_subagent_workflow_roles_subset_of_definitions() -> None:
    workflow = PROMPT_SECTIONS.subagent_workflow
    user_selectable = [n for n in AGENT_DEFINITIONS if n != "checkpoint-writer"]
    for name in user_selectable:
        assert name in workflow, f"role {name} missing from subagent_workflow section"
    # checkpoint-writer is system-dispatched; it must NOT be offered to the model.
    assert "checkpoint-writer" not in workflow
    assert "✅" not in workflow
    assert "❌" not in workflow


# ── TurnLoop wiring (P3) ──────────────────────────────────────────────────────
def test_engine_builder_injects_prompt_and_turnloop_consumes_it(tmp_path: Path) -> None:
    async def _run() -> str:
        opts = EngineOptions(model="echo", cwd=tmp_path, session_mode="one_shot")
        adapter = await EngineBuilder(opts).build()
        app = adapter._app  # type: ignore[attr-defined]
        assert app.prompt is not None
        rendered = app.prompt.render()
        # The loop's _system_prompt() returns the assembled base.
        base = adapter._loop._system_prompt()  # type: ignore[attr-defined]
        return rendered + "\x00" + base

    rendered, base = _run_async(_run).split("\x00")
    assert PROMPT_SECTIONS.intro in rendered
    assert "不要反问" in rendered  # one_shot guidance threaded through
    assert PROMPT_SECTIONS.intro in base


def test_system_prompt_degrades_to_empty_without_assembler(tmp_path: Path) -> None:
    async def _run() -> str:
        opts = EngineOptions(model="echo", cwd=tmp_path)
        adapter = await EngineBuilder(opts).build()
        adapter._app.prompt = None  # type: ignore[attr-defined]
        return adapter._loop._system_prompt()  # type: ignore[attr-defined]

    assert _run_async(_run) == ""


def _run_async(fn):  # type: ignore[no-untyped-def]
    return asyncio.run(fn())
