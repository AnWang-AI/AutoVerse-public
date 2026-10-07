"""gap4 (Skills): SKILL.md frontmatter loader + SkillTool model invocation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from autoverse.infra.skills.loader import (
    load_skills_dir,
    load_skills_dir_with_diagnostics,
    parse_frontmatter,
    skill_from_markdown,
)
from autoverse.infra.skills.registry import SkillRegistry
from autoverse.infra.skills.tool import SkillArgs, SkillTool

from tests.conftest import make_ctx

_DOC = """---
name: greet
description: Greet someone
---
Say hello in a friendly tone.
"""


def test_parse_frontmatter_splits_front_and_body() -> None:
    front, body = parse_frontmatter(_DOC)
    assert front["name"] == "greet"
    assert front["description"] == "Greet someone"
    assert "Say hello" in body


def test_no_frontmatter_returns_whole_body() -> None:
    front, body = parse_frontmatter("just a body, no front")
    assert front == {} and body == "just a body, no front"


def test_pure_fallback_parser_without_pyyaml(monkeypatch: Any) -> None:
    # Force the PyYAML import to fail so the pure fallback path runs.
    monkeypatch.setitem(sys.modules, "yaml", None)
    front, _ = parse_frontmatter(_DOC)
    assert front["name"] == "greet"
    assert front["description"] == "Greet someone"


def test_skill_from_markdown_fields() -> None:
    s = skill_from_markdown(_DOC, default_name="x")
    assert s.name == "greet"
    assert s.description == "Greet someone"
    assert "Say hello" in s.body


def test_skill_from_markdown_ignores_unknown_frontmatter() -> None:
    doc = _DOC.replace("---\nSay", "x-autoverse-old: ignored\n---\nSay")

    skill = skill_from_markdown(doc, default_name="x")

    assert skill.name == "greet"
    assert skill.description == "Greet someone"


def test_load_skills_dir_discovers_recursively(tmp_path: Path) -> None:
    d = tmp_path / "greet"
    d.mkdir()
    (d / "SKILL.md").write_text(_DOC, encoding="utf-8")
    skills = load_skills_dir(tmp_path)
    assert len(skills) == 1 and skills[0].name == "greet"


def test_load_skills_dir_reports_invalid_specs(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("missing frontmatter", encoding="utf-8")
    skills, diagnostics = load_skills_dir_with_diagnostics(tmp_path)
    assert skills == []
    assert diagnostics and diagnostics[0].code == "invalid_skill_spec"


def test_load_skills_dir_loads_portable_skill_by_name(tmp_path: Path) -> None:
    d = tmp_path / "excalidraw-diagram"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: excalidraw-diagram\ndescription: Draw diagrams\n---\nBody\n",
        encoding="utf-8",
    )

    skills, diagnostics = load_skills_dir_with_diagnostics(tmp_path)

    assert [s.name for s in skills] == ["excalidraw-diagram"]
    assert diagnostics == []


def test_load_skills_dir_skips_vendor_directories(tmp_path: Path) -> None:
    d = tmp_path / "excalidraw-diagram"
    vendor = d / "references" / ".venv" / "lib" / "python3.14" / "site-packages" / "pkg"
    vendor.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: excalidraw-diagram\ndescription: Draw diagrams\n---\nBody\n",
        encoding="utf-8",
    )
    (vendor / "SKILL.md").write_text(
        "---\nname: playwright-trace\ndescription: Vendor skill\n---\nBody\n",
        encoding="utf-8",
    )

    skills, diagnostics = load_skills_dir_with_diagnostics(tmp_path)

    assert [s.name for s in skills] == ["excalidraw-diagram"]
    assert diagnostics == []


# ── SkillTool ─────────────────────────────────────────────────────────────────

async def test_skill_tool_inline_renders_body(tmp_path: Path) -> None:
    reg = SkillRegistry()
    reg.register(skill_from_markdown(_DOC))
    tool = SkillTool(reg)
    ctx = make_ctx(tmp_path)
    out = await tool.run(SkillArgs(skill="greet", args="Alice"), ctx)
    assert not out.is_error
    assert "Say hello in a friendly tone." in out.output
    assert "User arguments: Alice" in out.output
    assert out.metadata["mode"] == "inline"


async def test_skill_tool_unknown_errors(tmp_path: Path) -> None:
    tool = SkillTool(SkillRegistry())
    out = await tool.run(SkillArgs(skill="nope"), make_ctx(tmp_path))
    assert out.is_error and out.error_kind == "unknown_skill"


def test_contributed_tools_returns_skill_tool() -> None:
    reg = SkillRegistry()
    reg.register(skill_from_markdown(_DOC))
    tools = reg.contributed_tools()
    assert len(tools) == 1 and tools[0].name == "Skill"
    # empty registry contributes nothing
    assert SkillRegistry().contributed_tools() == []
