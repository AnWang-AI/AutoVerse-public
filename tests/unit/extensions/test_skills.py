"""Unit tests for SkillRegistry (docs/reference/skills.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.infra.skills import Skill, SkillRegistry, render_available_skills
from autoverse.infra.skills.spec import SkillDiagnostic


def _skill(name: str, **kw: object) -> Skill:
    return Skill(name=name, description=kw.pop("description", f"{name} desc"), **kw)  # type: ignore[arg-type]


def test_description_quoted_phrase_match() -> None:
    reg = SkillRegistry()
    reg.register(
        _skill("simplify", description="Use when the user says 'simplify this'.")
    )
    assert [s.name for s in reg.match("please simplify this code")] == ["simplify"]
    assert reg.match("totally unrelated request") == []


def test_description_keyword_match() -> None:
    reg = SkillRegistry()
    reg.register(_skill("outline", description="Generate a paper outline structure"))
    assert [s.name for s in reg.match("help me outline the paper")] == ["outline"]


def test_empty_description_never_matches() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="noop"))
    assert reg.match("noop please") == []


def test_register_duplicate_raises_no_silent_overwrite() -> None:
    reg = SkillRegistry()
    reg.register(_skill("dup", description="first"))
    with pytest.raises(ValueError, match="duplicate skill name"):
        reg.register(_skill("dup", description="second"))
    # First-wins: the original survives.
    assert reg.get("dup").description == "first"  # type: ignore[union-attr]
    # Overwrite is explicit opt-in.
    reg.register(_skill("dup", description="third"), overwrite=True)
    assert reg.get("dup").description == "third"  # type: ignore[union-attr]


def test_namespace_alias_and_unique_enabled() -> None:
    reg = SkillRegistry()
    reg.register_from_dir("acme", [_skill("build")])
    # Both the bare and qualified keys resolve to the same skill.
    assert reg.get("build") is reg.get("acme:build")
    # unique_enabled_skills collapses the alias to one entry.
    assert [s.name for s in reg.unique_enabled_skills()] == ["build"]


def test_loaded_skills_are_available_for_any_cwd(tmp_path: Path) -> None:
    reg = SkillRegistry()
    reg.register(_skill("frontend", description="frontend work"))
    section = reg.available_section(cwd=tmp_path)
    assert "**frontend**" in section


def test_available_section_derived_from_registry() -> None:
    reg = SkillRegistry()
    assert reg.available_section() == ""  # empty registry -> empty section
    reg.register(_skill("a", description="alpha"))
    section = reg.available_section()
    assert "## Available Skills" in section
    assert "**a** [user]: alpha" in section


def test_prompt_fragment_reports_invalid_discovered_skills(tmp_path: Path) -> None:
    reg = SkillRegistry()
    reg.add_diagnostic(
        SkillDiagnostic(
            code="invalid_skill_spec",
            message="invalid skill name 'Excalidraw Diagram'",
            path=tmp_path / "excalidraw-diagram" / "SKILL.md",
            skill="excalidraw-diagram",
        )
    )

    section = reg.prompt_fragment()

    assert "## Skill Diagnostics" in section
    assert "excalidraw-diagram" in section
    assert "invalid skill name" in section


def test_render_skill_body_appends_explicit_arguments() -> None:
    s = Skill(name="cite", body="Cite the provided paper.")
    assert s.render("paperA extra") == "Cite the provided paper.\n\nUser arguments: paperA extra"


def test_command_descriptors_include_loaded_skills() -> None:
    reg = SkillRegistry()
    reg.register(_skill("visible"))
    reg.register(_skill("another"))
    names = [d.name for d in reg.command_descriptors()]
    assert names == ["visible", "another"]


def test_extension_source_shape() -> None:
    reg = SkillRegistry()
    assert reg.name == "skills"
    assert reg.contributed_tools() == []
