"""Unit tests for frontend-agnostic extension administration."""

from __future__ import annotations

from pathlib import Path

from autoverse.app.extension_admin import ExtensionAdminService
from autoverse.infra.plugins import Plugin, PluginManager, PluginManifest
from autoverse.infra.skills.spec import SkillDiagnostic
from autoverse.infra.skills import Skill, SkillRegistry


def test_extension_admin_lists_and_diagnoses_skills(tmp_path: Path) -> None:
    reg = SkillRegistry(cwd=tmp_path)
    reg.register(Skill(name="review", description="Review code"))
    admin = ExtensionAdminService(skills=reg, plugins=None, user_skill_root=tmp_path)

    items = admin.list_skills()
    assert items[0].id == "review"
    assert "matches prompt: True" in admin.diagnose_skill("review", "please review code")

    admin.disable_skill("review")
    assert admin.list_skills()[0].state == "disabled"
    admin.enable_skill("review")
    assert admin.list_skills()[0].state == "enabled"


def test_extension_admin_lists_invalid_discovered_skills(tmp_path: Path) -> None:
    skill_path = tmp_path / "excalidraw-diagram" / "SKILL.md"
    reg = SkillRegistry(cwd=tmp_path)
    reg.add_diagnostic(
        SkillDiagnostic(
            code="invalid_skill_spec",
            message="invalid skill name 'Excalidraw Diagram'",
            path=skill_path,
            skill="excalidraw-diagram",
        )
    )
    admin = ExtensionAdminService(skills=reg, plugins=None, user_skill_root=tmp_path)

    items = admin.list_skills()

    assert items[0].id == "excalidraw-diagram"
    assert items[0].state == "invalid"
    assert items[0].diagnostics_count == 1
    assert "invalid_skill_spec" in admin.diagnose_skill("excalidraw-diagram")


async def test_extension_admin_lists_plugins(tmp_path: Path) -> None:
    mgr = PluginManager(
        entry_point_loader=lambda: [
            Plugin(
                manifest=PluginManifest(
                    name="p",
                    provides=frozenset({"skills"}),
                ),
                root=tmp_path,
                skills=[Skill(name="s", description="S")],
            )
        ]
    )
    await mgr.setup()
    admin = ExtensionAdminService(skills=None, plugins=mgr, user_skill_root=tmp_path)

    items = admin.list_plugins()
    assert items[0].id == "p"
    assert "plugin: p" in admin.diagnose_plugin("p")

    admin.disable_plugin("p")
    assert not admin.list_plugins()[0].enabled
