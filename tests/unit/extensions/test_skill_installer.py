"""Unit tests for standalone skill package installation (skills.md)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.skills.installer import install_skill_package


_DOC = """---
name: diagram
description: Draw diagrams
---
Draw a diagram.
"""


def test_install_single_skill_package(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(_DOC, encoding="utf-8")
    vendor = src / "references" / ".venv" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "junk.py").write_text("x", encoding="utf-8")
    dest = tmp_path / "installed"

    result = install_skill_package(src, dest)

    assert result.diagnostics == ()
    assert len(result.records) == 1
    assert result.records[0].name == "diagram"
    assert (dest / "diagram" / "SKILL.md").is_file()
    assert not (dest / "diagram" / "references" / ".venv").exists()


def test_install_collection_package(tmp_path: Path) -> None:
    src = tmp_path / "collection"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    (src / "a" / "SKILL.md").write_text(_DOC.replace("diagram", "alpha"), encoding="utf-8")
    (src / "b" / "SKILL.md").write_text(_DOC.replace("diagram", "beta"), encoding="utf-8")

    result = install_skill_package(src, tmp_path / "installed")

    assert sorted(r.name for r in result.records) == ["alpha", "beta"]


def test_install_reports_missing_skill(tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    result = install_skill_package(src, tmp_path / "installed")

    assert result.records == ()
    assert result.diagnostics[0].code == "no_skill_package"
