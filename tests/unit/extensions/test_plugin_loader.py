"""Unit tests for Codex plugin directory loading."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.plugins.loader import load_plugin_dirs, load_plugin_spec
from autoverse.infra.plugins.manager import PluginManager


_SKILL = """---
name: review-diff
description: Review a diff
---
Review the diff.
"""


def _write_plugin(root: Path) -> Path:
    plugin = root / "review-pack"
    (plugin / "skills" / "review-diff").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        """
{
  "name": "review-pack",
  "version": "1.0.0",
  "description": "Review workflows",
  "author": {"name": "tests"},
  "skills": "./skills/"
}
""".strip(),
        encoding="utf-8",
    )
    (plugin / "skills" / "review-diff" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return plugin


def test_load_plugin_spec(tmp_path: Path) -> None:
    plugin = _write_plugin(tmp_path)
    spec = load_plugin_spec(plugin / ".codex-plugin" / "plugin.json")
    assert spec.id == "review-pack"
    assert spec.capabilities.skills
    assert spec.install_key == "review-pack@tests"


async def test_plugin_manager_scans_directory_plugins(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    mgr = PluginManager(plugin_dirs=[tmp_path])

    await mgr.setup()

    assert mgr.plugin_status["review-pack"].state == "loaded"
    skills = mgr.contributed_skills()["review-pack"]
    assert [s.name for s in skills] == ["review-diff"]
    assert skills[0].source == "plugin"


def test_load_plugin_dirs_ignores_non_plugins(tmp_path: Path) -> None:
    (tmp_path / "not-plugin").mkdir()
    assert load_plugin_dirs([tmp_path]) == []


def test_load_plugin_dirs_ignores_legacy_plugin_toml(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "plugin.toml").write_text(
        'name = "legacy"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    assert load_plugin_dirs([tmp_path]) == []
